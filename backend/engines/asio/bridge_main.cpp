// A&D Voice ASIO bridge (GPLv3 option of the Steinberg ASIO SDK).
//
// The bridge is intentionally a small, independent process.  ASIO drivers are
// vendor-native DLLs: isolating them keeps a driver failure from terminating
// the FastAPI backend or the Electron UI.
#include <windows.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstring>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "asiosys.h"
#include "asio.h"
#include "asiodrivers.h"

extern bool loadAsioDriver(char* name);

namespace {

constexpr long kMaxInputChannels = 8;
constexpr long kMaxOutputChannels = 2;
constexpr long kMaxBuffers = kMaxInputChannels + kMaxOutputChannels;
// Two overlap-add read heads make the audible algorithmic history roughly
// half this window.  Twelve milliseconds keeps octave monitoring below 8 ms
// of app-added latency while retaining a complete period for an ~83 Hz voice.
constexpr double kPitchWindowSeconds = 0.012;
// std::tanh is not constexpr; this still runs exactly once at static
// initialization, not per sample like the inline std::tanh(1.12F) it
// replaces in process_effects() below.
const float kGateNormalizationTanh = std::tanh(1.12F);
std::atomic_bool g_running{true};
// Set only from kAsioResetRequest (asio_message), never from a host-initiated
// stop (Ctrl+C, or the parent process terminating this one) -- lets the host
// tell those two "the stream stopped" cases apart in the final "stopped"
// event, so it can reopen the driver (as the ASIO SDK's own comment on
// kAsioResetRequest instructs) instead of reporting a monitoring failure.
std::atomic_bool g_reset_requested{false};
std::atomic<float> g_rms{-120.0F};
std::atomic_bool g_clipping{false};
AsioDrivers* g_drivers = nullptr;

struct Options {
  std::string driver_name;
  long input_channel = -1;
  long output_channel = -1;
  long output_channels = 2;
  long buffer_size = 0;
  double sample_rate = 0.0;
  double gain = 1.0;
  double reverb = 0.0;
  double echo = 0.0;
  double delay = 0.0;
  double noise_suppression = 0.35;
  double octave = 0.0;
  bool list = false;
};

struct Engine {
  ASIODriverInfo info{};
  ASIOCallbacks callbacks{};
  ASIOBufferInfo buffers[kMaxBuffers]{};
  ASIOChannelInfo channels[kMaxBuffers]{};
  long input_count = 0;
  long output_count = 0;
  long buffer_size = 0;
  long input_latency = -1;
  long output_latency = -1;
  float gain = 1.0F;
  float reverb = 0.0F;
  float echo = 0.0F;
  float delay = 0.0F;
  float noise_suppression = 0.35F;
  float octave = 0.0F;
  float previous_input = 0.0F;
  float highpass_state = 0.0F;
  float noise_floor = 0.003F;
  float gate_gain = 1.0F;
  float sample_rate = 44100.0F;
  // Depend only on sample_rate/octave/pitch_line size, all of which are fixed
  // once at create_buffers() and never change afterward (this process is
  // restarted, not live-reconfigured, when settings change -- see
  // audio_service._start_asio_monitor). Precomputed there instead of being
  // recomputed by process_effects()/process_pitch() on every single sample.
  float highpass_coefficient = 0.0F;
  double pitch_phase_increment = 0.0;
  std::vector<float> reverb_line_a;
  std::vector<float> reverb_line_b;
  std::vector<float> echo_line;
  std::vector<float> delay_line;
  std::vector<float> monitor_scratch;
  std::vector<float> pitch_line;
  size_t effect_cursor = 0;
  size_t pitch_cursor = 0;
  double pitch_phase = 0.0;
  bool output_ready = false;
  bool buffers_created = false;
  long active_input_channel = -1;
} g_engine;

void emit(const std::string& json) { std::cout << json << std::endl; }

std::string escape_json(const std::string& value) {
  std::string result;
  for (const char character : value) {
    if (character == '\\' || character == '"') result.push_back('\\');
    if (character == '\n') result += "\\n";
    else result.push_back(character);
  }
  return result;
}

std::optional<Options> parse_options(int argc, char** argv) {
  Options options;
  for (int index = 1; index < argc; ++index) {
    const std::string key = argv[index];
    if (key == "--list") options.list = true;
    else if (key == "--driver" && index + 1 < argc) options.driver_name = argv[++index];
    else if (key == "--input-channel" && index + 1 < argc) options.input_channel = std::stol(argv[++index]);
    else if (key == "--output-channel" && index + 1 < argc) options.output_channel = std::stol(argv[++index]);
    else if (key == "--output-channels" && index + 1 < argc) options.output_channels = std::stol(argv[++index]);
    else if (key == "--buffer-size" && index + 1 < argc) options.buffer_size = std::stol(argv[++index]);
    else if (key == "--sample-rate" && index + 1 < argc) options.sample_rate = std::stod(argv[++index]);
    else if (key == "--gain" && index + 1 < argc) options.gain = std::stod(argv[++index]);
    else if (key == "--reverb" && index + 1 < argc) options.reverb = std::stod(argv[++index]);
    else if (key == "--echo" && index + 1 < argc) options.echo = std::stod(argv[++index]);
    else if (key == "--delay" && index + 1 < argc) options.delay = std::stod(argv[++index]);
    else if (key == "--noise-suppression" && index + 1 < argc)
      options.noise_suppression = std::stod(argv[++index]);
    else if (key == "--octave" && index + 1 < argc) options.octave = std::stod(argv[++index]);
    else return std::nullopt;
  }
  return options;
}

void __cdecl stop_handler(int) { g_running = false; }
BOOL WINAPI console_handler(DWORD signal) {
  if (signal == CTRL_C_EVENT || signal == CTRL_BREAK_EVENT || signal == CTRL_CLOSE_EVENT) {
    g_running = false;
    return TRUE;
  }
  return FALSE;
}

int bytes_per_sample(ASIOSampleType type) {
  switch (type) {
    case ASIOSTInt16LSB:
    case ASIOSTInt16MSB: return 2;
    case ASIOSTInt24LSB:
    case ASIOSTInt24MSB: return 3;
    case ASIOSTInt32LSB:
    case ASIOSTInt32MSB:
    case ASIOSTFloat32LSB:
    case ASIOSTFloat32MSB:
    case ASIOSTInt32LSB16:
    case ASIOSTInt32LSB18:
    case ASIOSTInt32LSB20:
    case ASIOSTInt32LSB24:
    case ASIOSTInt32MSB16:
    case ASIOSTInt32MSB18:
    case ASIOSTInt32MSB20:
    case ASIOSTInt32MSB24: return 4;
    case ASIOSTFloat64LSB:
    case ASIOSTFloat64MSB: return 8;
    default: return 0;
  }
}

// Int24LSB is a plain 3-byte little-endian signed PCM sample -- unambiguous
// and common on real interfaces (unlike the ASIOSTInt32LSB16/18/20/24 and
// MSB-endian variants below, whose exact bit alignment isn't worth guessing
// at without the hardware to verify against; those deliberately fall back to
// the safe copy-without-gain/silence paths in copy_with_gain/process_buffer
// instead of risking wrong sample math on real audio).
int32_t read_int24(const uint8_t* bytes) {
  int32_t value = (static_cast<int32_t>(bytes[0])) | (static_cast<int32_t>(bytes[1]) << 8) |
                  (static_cast<int32_t>(bytes[2]) << 16);
  if (value & 0x00800000) value |= static_cast<int32_t>(0xFF000000);  // sign-extend
  return value;
}

void write_int24(uint8_t* bytes, int32_t value) {
  bytes[0] = static_cast<uint8_t>(value & 0xFF);
  bytes[1] = static_cast<uint8_t>((value >> 8) & 0xFF);
  bytes[2] = static_cast<uint8_t>((value >> 16) & 0xFF);
}

float sample_peak(const void* source, ASIOSampleType type, long frames) {
  float peak = 0.0F;
  if (type == ASIOSTFloat32LSB) {
    const auto* values = static_cast<const float*>(source);
    for (long index = 0; index < frames; ++index) peak = std::max(peak, std::abs(values[index]));
  } else if (type == ASIOSTInt32LSB || type == ASIOSTInt32LSB24) {
    const auto* values = static_cast<const int32_t*>(source);
    for (long index = 0; index < frames; ++index) {
      peak = std::max(peak, static_cast<float>(std::abs(static_cast<double>(values[index])) / 2147483648.0));
    }
  } else if (type == ASIOSTInt16LSB) {
    const auto* values = static_cast<const int16_t*>(source);
    for (long index = 0; index < frames; ++index) peak = std::max(peak, std::abs(values[index] / 32768.0F));
  } else if (type == ASIOSTInt24LSB) {
    const auto* bytes = static_cast<const uint8_t*>(source);
    for (long index = 0; index < frames; ++index)
      peak = std::max(peak, std::abs(static_cast<float>(read_int24(bytes + index * 3) / 8388608.0)));
  }
  return peak;
}

float decode_sample(const void* source, ASIOSampleType type, long index) {
  if (type == ASIOSTFloat32LSB) return static_cast<const float*>(source)[index];
  if (type == ASIOSTInt32LSB || type == ASIOSTInt32LSB24)
    return static_cast<float>(static_cast<const int32_t*>(source)[index] / 2147483648.0);
  if (type == ASIOSTInt16LSB) return static_cast<const int16_t*>(source)[index] / 32768.0F;
  if (type == ASIOSTInt24LSB)
    return static_cast<float>(read_int24(static_cast<const uint8_t*>(source) + index * 3) / 8388608.0);
  return 0.0F;
}

void encode_sample(void* target, ASIOSampleType type, long index, float sample) {
  const float clipped = std::clamp(sample, -1.0F, 1.0F);
  if (type == ASIOSTFloat32LSB) static_cast<float*>(target)[index] = clipped;
  else if (type == ASIOSTInt32LSB || type == ASIOSTInt32LSB24)
    static_cast<int32_t*>(target)[index] = static_cast<int32_t>(clipped * 2147483647.0F);
  else if (type == ASIOSTInt16LSB)
    static_cast<int16_t*>(target)[index] = static_cast<int16_t>(clipped * 32767.0F);
  else if (type == ASIOSTInt24LSB)
    write_int24(static_cast<uint8_t*>(target) + index * 3, static_cast<int32_t>(clipped * 8388607.0F));
}

// True RMS, matching what the Python/WASAPI monitoring paths report under
// the same "rms_db" key -- the previous implementation here was actually a
// peak-hold envelope (see g_rms's old update in process_buffer), so this
// engine's meter read differently from the others for the same signal.
float sample_rms(const void* source, ASIOSampleType type, long frames) {
  if (frames <= 0) return 0.0F;
  double sum_squares = 0.0;
  for (long index = 0; index < frames; ++index) {
    const float sample = decode_sample(source, type, index);
    sum_squares += static_cast<double>(sample) * sample;
  }
  return static_cast<float>(std::sqrt(sum_squares / frames));
}

bool supports_dsp(ASIOSampleType type) {
  return type == ASIOSTFloat32LSB || type == ASIOSTInt32LSB ||
         type == ASIOSTInt32LSB24 || type == ASIOSTInt16LSB || type == ASIOSTInt24LSB;
}

// Mirrors the exact thresholds process_effects() itself uses to skip
// reverb/echo/delay/pitch -- when every one of them is a no-op, running the
// whole per-sample chain (highpass filter, noise gate smoothing, tanh
// saturation) just to reproduce the input is pure realtime-thread cost with
// no audible effect. copy_with_gain still applies gain.
bool effects_enabled() {
  return g_engine.reverb > 0.001F || g_engine.echo > 0.001F || g_engine.delay > 0.001F ||
         std::abs(g_engine.octave) > 0.005F || g_engine.noise_suppression > 0.001F;
}

float process_pitch(float sample) {
  if (std::abs(g_engine.octave) < 0.005F || g_engine.pitch_line.empty()) return sample;
  auto& line = g_engine.pitch_line;
  const size_t length = line.size();
  line[g_engine.pitch_cursor] = sample;
  const double phase_a = std::fmod(g_engine.pitch_phase + 1.0, 1.0);
  const double phase_b = std::fmod(phase_a + 0.5, 1.0);
  const auto read = [&](double phase) {
    double position = static_cast<double>(g_engine.pitch_cursor) - 1.0 -
                      phase * static_cast<double>(length - 2);
    while (position < 0.0) position += static_cast<double>(length);
    const size_t left = static_cast<size_t>(position) % length;
    const size_t right = (left + 1) % length;
    const float fraction = static_cast<float>(position - std::floor(position));
    return line[left] * (1.0F - fraction) + line[right] * fraction;
  };
  const float weight_a = static_cast<float>(
    0.5 - 0.5 * std::cos(2.0 * 3.141592653589793 * phase_a));
  const float shifted = read(phase_a) * weight_a + read(phase_b) * (1.0F - weight_a);
  g_engine.pitch_phase = std::fmod(
    g_engine.pitch_phase + g_engine.pitch_phase_increment + 1.0, 1.0);
  g_engine.pitch_cursor = (g_engine.pitch_cursor + 1) % length;
  return shifted;
}

float process_effects(float sample) {
  const float input = sample * g_engine.gain;
  const float highpass = input - g_engine.previous_input +
                         g_engine.highpass_coefficient * g_engine.highpass_state;
  g_engine.previous_input = input;
  g_engine.highpass_state = highpass;
  const float level = std::abs(highpass);
  if (level < g_engine.noise_floor * 1.4F)
    g_engine.noise_floor = g_engine.noise_floor * 0.9995F + level * 0.0005F;
  const float threshold = std::max(0.0025F, g_engine.noise_floor * 2.4F);
  const float suppressed = level <= threshold
    ? 0.12F + 0.38F * (threshold > 0.0F ? level / threshold : 0.0F)
    : level < threshold * 2.2F
      ? 0.5F + 0.5F * (level - threshold) / (threshold * 1.2F)
      : 1.0F;
  const float target_gate = 1.0F + (suppressed - 1.0F) * g_engine.noise_suppression;
  const float gate_speed = target_gate > g_engine.gate_gain ? 0.08F : 0.02F;
  g_engine.gate_gain += (target_gate - g_engine.gate_gain) * gate_speed;
  const float dry = process_pitch(
    std::tanh(highpass * g_engine.gate_gain * 1.12F) / kGateNormalizationTanh);
  const size_t cursor = g_engine.effect_cursor % g_engine.reverb_line_a.size();
  const auto read_delay = [cursor](const std::vector<float>& line, size_t samples) {
    const size_t offset = std::min(samples, line.size() - 1);
    return line[(cursor + line.size() - offset) % line.size()];
  };
  float result = dry;
  if (g_engine.reverb > 0.001F) {
    const float early = read_delay(g_engine.reverb_line_a,
      static_cast<size_t>(g_engine.sample_rate * 0.037F));
    const float late = read_delay(g_engine.reverb_line_b,
      static_cast<size_t>(g_engine.sample_rate * 0.091F));
    g_engine.reverb_line_a[cursor] = dry + early * 0.42F;
    g_engine.reverb_line_b[cursor] = early + late * 0.28F;
    result += (early * 0.42F + late * 0.26F) * g_engine.reverb;
  }
  if (g_engine.echo > 0.001F) {
    const float echoed = read_delay(g_engine.echo_line,
      static_cast<size_t>(g_engine.sample_rate * 0.235F));
    g_engine.echo_line[cursor] = dry + echoed * (0.16F + g_engine.echo * 0.34F);
    result += echoed * (0.12F + g_engine.echo * 0.34F);
  }
  if (g_engine.delay > 0.001F) {
    const float delayed = read_delay(g_engine.delay_line,
      static_cast<size_t>(g_engine.sample_rate * (0.12F + g_engine.delay * 0.38F)));
    g_engine.delay_line[cursor] = dry + delayed * 0.22F;
    result += delayed * (0.10F + g_engine.delay * 0.30F);
  }
  ++g_engine.effect_cursor;
  return std::clamp(result, -1.0F, 1.0F);
}

// ASIO buffers are passed directly from the callback. Keep all DSP allocation
// out of this thread: delay lines are prepared once in create_buffers().
void copy_with_gain(void* target, const void* source, ASIOSampleType type, long frames, float gain) {
  if (gain == 1.0F) {
    std::memcpy(target, source, static_cast<size_t>(frames * bytes_per_sample(type)));
    return;
  }
  if (type == ASIOSTFloat32LSB) {
    const auto* input = static_cast<const float*>(source);
    auto* output = static_cast<float*>(target);
    for (long index = 0; index < frames; ++index) output[index] = std::clamp(input[index] * gain, -1.0F, 1.0F);
    return;
  }
  if (type == ASIOSTInt32LSB || type == ASIOSTInt32LSB24) {
    const auto* input = static_cast<const int32_t*>(source);
    auto* output = static_cast<int32_t*>(target);
    for (long index = 0; index < frames; ++index) {
      const double scaled = std::clamp(static_cast<double>(input[index]) * gain, -2147483648.0, 2147483647.0);
      output[index] = static_cast<int32_t>(scaled);
    }
    return;
  }
  if (type == ASIOSTInt16LSB) {
    const auto* input = static_cast<const int16_t*>(source);
    auto* output = static_cast<int16_t*>(target);
    for (long index = 0; index < frames; ++index) {
      output[index] = static_cast<int16_t>(std::clamp(static_cast<float>(input[index]) * gain, -32768.0F, 32767.0F));
    }
    return;
  }
  // Unsupported packed/endian formats are preserved rather than corrupted.
  std::memcpy(target, source, static_cast<size_t>(frames * bytes_per_sample(type)));
}

void copy_with_effects(void* target, const void* source, ASIOSampleType type, long frames) {
  for (long index = 0; index < frames; ++index)
    encode_sample(target, type, index, process_effects(decode_sample(source, type, index)));
}

void process_buffer(long buffer_index) {
  if (g_engine.input_count < 1 || g_engine.output_count < 1) return;
  const void* input = nullptr;
  ASIOSampleType input_type = ASIOSTLastEntry;
  long selected_channel = -1;
  float peak = -1.0F;
  // ASIO driver channel numbering is independent from Windows endpoint IDs.
  // In automatic mode retain all practical input channels and use the one
  // carrying the strongest block, so a Realtek microphone is not assumed to
  // be channel zero (which is often Stereo Mix or an inactive jack).
  for (long channel = 0; channel < g_engine.input_count; ++channel) {
    const void* candidate = g_engine.buffers[channel].buffers[buffer_index];
    const auto type = g_engine.channels[channel].type;
    if (!candidate || bytes_per_sample(type) == 0) continue;
    const float candidate_peak = sample_peak(candidate, type, g_engine.buffer_size);
    if (candidate_peak > peak) { input = candidate; input_type = type; peak = candidate_peak; selected_channel = channel; }
  }
  const int input_bytes = bytes_per_sample(input_type);
  if (input == nullptr || input_bytes == 0) return;

  if (selected_channel != g_engine.active_input_channel) {
    // Automatic channel selection just switched which physical input feeds
    // the DSP chain -- every one of these carries state built up from the
    // PREVIOUS channel's audio (highpass/gate history, noise floor, pitch
    // buffer, reverb/echo/delay tails). Reset them instead of processing the
    // new channel's first blocks against another input's history, which was
    // audible as a click/gate hiccup/stray effect tail on every auto-switch.
    g_engine.active_input_channel = selected_channel;
    g_engine.previous_input = 0.0F;
    g_engine.highpass_state = 0.0F;
    g_engine.noise_floor = 0.003F;
    g_engine.gate_gain = 1.0F;
    std::fill(g_engine.reverb_line_a.begin(), g_engine.reverb_line_a.end(), 0.0F);
    std::fill(g_engine.reverb_line_b.begin(), g_engine.reverb_line_b.end(), 0.0F);
    std::fill(g_engine.echo_line.begin(), g_engine.echo_line.end(), 0.0F);
    std::fill(g_engine.delay_line.begin(), g_engine.delay_line.end(), 0.0F);
    std::fill(g_engine.pitch_line.begin(), g_engine.pitch_line.end(), 0.0F);
    g_engine.pitch_cursor = 0;
    g_engine.pitch_phase = 0.0;
  }

  const float rms = sample_rms(input, input_type, g_engine.buffer_size);
  g_rms = rms > 0.0F ? 20.0F * std::log10(rms) : -120.0F;
  g_clipping = peak >= 0.99F;

  const bool use_dsp = effects_enabled() && supports_dsp(input_type);
  if (use_dsp) {
    auto& processed = g_engine.monitor_scratch;
    for (long index = 0; index < g_engine.buffer_size; ++index)
      processed[static_cast<size_t>(index)] = process_effects(decode_sample(input, input_type, index));
  }
  for (long output = 0; output < g_engine.output_count; ++output) {
    const long output_index = g_engine.input_count + output;
    void* target = g_engine.buffers[output_index].buffers[buffer_index];
    const ASIOSampleType output_type = g_engine.channels[output_index].type;
    if (target == nullptr) continue;
    if (use_dsp && supports_dsp(output_type)) {
      for (long index = 0; index < g_engine.buffer_size; ++index)
        encode_sample(target, output_type, index, g_engine.monitor_scratch[static_cast<size_t>(index)]);
    } else if (output_type == input_type) {
      copy_with_gain(target, input, input_type, g_engine.buffer_size, g_engine.gain);
    } else if (supports_dsp(input_type) && supports_dsp(output_type)) {
      for (long index = 0; index < g_engine.buffer_size; ++index)
        encode_sample(target, output_type, index,
                      decode_sample(input, input_type, index) * g_engine.gain);
    } else {
      std::memset(target, 0, static_cast<size_t>(g_engine.buffer_size * bytes_per_sample(output_type)));
    }
  }
  if (g_engine.output_ready) ASIOOutputReady();
}

void buffer_switch(long index, ASIOBool) { process_buffer(index); }
ASIOTime* buffer_switch_time_info(ASIOTime* time_info, long index, ASIOBool) {
  process_buffer(index);
  return time_info;
}
// Every DSP buffer (highpass coefficient, reverb/echo/delay lines, pitch
// line/phase) is sized and tuned for one specific sample rate. Shared by
// create_buffers() (first open) and sample_rate_changed() (the driver
// switching clocks live, e.g. 44.1 <-> 48kHz) so a runtime rate change can
// never leave DSP state computed for the old rate silently applied to audio
// running at the new one -- that used to mean wrong delay/pitch timing until
// a full restart. Resets in-flight reverb/echo/delay tails (a brief, far
// less disruptive artifact than that).
void apply_sample_rate(double rate) {
  g_engine.sample_rate = static_cast<float>(std::max(1.0, rate));
  g_engine.highpass_coefficient = std::exp(-2.0F * 3.14159265F * 70.0F / g_engine.sample_rate);
  const size_t effect_size = static_cast<size_t>(std::max(1.0, rate * 0.72));
  g_engine.reverb_line_a.assign(effect_size, 0.0F);
  g_engine.reverb_line_b.assign(effect_size, 0.0F);
  g_engine.echo_line.assign(effect_size, 0.0F);
  g_engine.delay_line.assign(effect_size, 0.0F);
  g_engine.pitch_line.assign(
    static_cast<size_t>(std::max(96.0, rate * kPitchWindowSeconds)), 0.0F);
  const double pitch_ratio = std::pow(2.0, static_cast<double>(g_engine.octave));
  g_engine.pitch_phase_increment = (1.0 - pitch_ratio) / static_cast<double>(g_engine.pitch_line.size());
  g_engine.effect_cursor = 0;
  g_engine.pitch_cursor = 0;
  g_engine.pitch_phase = 0.0;
}

void sample_rate_changed(ASIOSampleRate new_rate) {
  // ASIO callbacks (buffer_switch, this one) are serialized on the same
  // driver thread, so this never races process_buffer() reading the same
  // g_engine state.
  if (new_rate > 0.0 && std::abs(new_rate - static_cast<double>(g_engine.sample_rate)) > 0.5)
    apply_sample_rate(new_rate);
}
long asio_message(long selector, long value, void*, double*) {
  if (selector == kAsioSelectorSupported) {
    return value == kAsioEngineVersion || value == kAsioSupportsTimeInfo || value == kAsioResetRequest ||
           value == kAsioResyncRequest || value == kAsioLatenciesChanged;
  }
  if (selector == kAsioEngineVersion) return 2;
  if (selector == kAsioResetRequest || selector == kAsioResyncRequest) {
    // kAsioResyncRequest means the driver's internal state may already be
    // corrupted (e.g. samples lost between hardware and driver) -- this
    // bridge has no lighter-weight internal resync, so treat it exactly like
    // a full reset instead of silently continuing on state the driver itself
    // flagged as suspect.
    g_reset_requested = true;
    emit("{\"event\":\"reset_requested\"}");
    g_running = false;
    return 1;
  }
  if (selector == kAsioLatenciesChanged) {
    // The driver changed something (e.g. its own control panel) that altered
    // latency without a full reset -- re-query and tell the caller, instead
    // of it silently keeping the stale figure from ASIOStart().
    long input_latency = 0, output_latency = 0;
    if (ASIOGetLatencies(&input_latency, &output_latency) == ASE_OK) {
      g_engine.input_latency = input_latency;
      g_engine.output_latency = output_latency;
      std::ostringstream update;
      update << "{\"event\":\"latency\",\"input_latency\":" << g_engine.input_latency
             << ",\"output_latency\":" << g_engine.output_latency << "}";
      emit(update.str());
    }
    return 1;
  }
  return 0;
}

long resolve_buffer_size(const Options& options, long minimum, long maximum, long preferred, long granularity) {
  if (options.buffer_size <= 0) return preferred;
  long size = std::clamp(options.buffer_size, minimum, maximum);
  if (granularity > 0) size = minimum + ((size - minimum) / granularity) * granularity;
  if (granularity == -1) { long power = 1; while (power < size) power <<= 1; size = power; }
  return std::clamp(size, minimum, maximum);
}

bool create_buffers(const Options& options) {
  long minimum = 0, maximum = 0, preferred = 0, granularity = 0;
  long available_inputs = 0, available_outputs = 0;
  if (ASIOGetChannels(&available_inputs, &available_outputs) != ASE_OK || available_inputs < 1 || available_outputs < 1) return false;
  if (options.input_channel >= available_inputs) return false;
  g_engine.input_count = options.input_channel < 0
    ? std::min(available_inputs, kMaxInputChannels) : 1;
  const long output_base = options.output_channel < 0 ? 0 : options.output_channel;
  if (output_base >= available_outputs) return false;
  g_engine.output_count = std::min({options.output_channels, available_outputs - output_base, kMaxOutputChannels});
  if (ASIOGetBufferSize(&minimum, &maximum, &preferred, &granularity) != ASE_OK) return false;
  g_engine.buffer_size = resolve_buffer_size(options, minimum, maximum, preferred, granularity);
  g_engine.gain = static_cast<float>(std::clamp(options.gain, 0.0, 4.0));
  g_engine.reverb = static_cast<float>(std::clamp(options.reverb, 0.0, 1.0));
  g_engine.echo = static_cast<float>(std::clamp(options.echo, 0.0, 1.0));
  g_engine.delay = static_cast<float>(std::clamp(options.delay, 0.0, 1.0));
  g_engine.noise_suppression = static_cast<float>(
    std::clamp(options.noise_suppression, 0.0, 1.0));
  g_engine.octave = static_cast<float>(std::clamp(options.octave, -1.0, 1.0));
  g_engine.monitor_scratch.assign(static_cast<size_t>(g_engine.buffer_size), 0.0F);
  ASIOSampleRate rate = 44100.0;
  ASIOGetSampleRate(&rate);
  apply_sample_rate(rate);
  for (long index = 0; index < g_engine.input_count; ++index) {
    g_engine.buffers[index].isInput = ASIOTrue;
    g_engine.buffers[index].channelNum = options.input_channel < 0 ? index : options.input_channel + index;
  }
  for (long index = 0; index < g_engine.output_count; ++index) {
    const long buffer_index = g_engine.input_count + index;
    g_engine.buffers[buffer_index].isInput = ASIOFalse;
    // Always channels 0/1 ignored the fact that a professional interface's
    // headphone/monitor pair is frequently NOT its first output pair (e.g.
    // outputs 3/4); --output-channel lets the caller pick the real base.
    g_engine.buffers[buffer_index].channelNum = output_base + index;
  }
  g_engine.callbacks = {buffer_switch, sample_rate_changed, asio_message, buffer_switch_time_info};
  const long count = g_engine.input_count + g_engine.output_count;
  if (ASIOCreateBuffers(g_engine.buffers, count, g_engine.buffer_size, &g_engine.callbacks) != ASE_OK) return false;
  g_engine.buffers_created = true;
  for (long index = 0; index < count; ++index) {
    g_engine.channels[index].channel = g_engine.buffers[index].channelNum;
    g_engine.channels[index].isInput = g_engine.buffers[index].isInput;
    if (ASIOGetChannelInfo(&g_engine.channels[index]) != ASE_OK || bytes_per_sample(g_engine.channels[index].type) == 0) return false;
  }
  g_engine.output_ready = ASIOOutputReady() == ASE_OK;
  return true;
}

void cleanup() {
  if (g_engine.buffers_created) ASIODisposeBuffers();
  ASIOExit();
  if (g_drivers != nullptr) g_drivers->removeCurrentDriver();
}

int list_drivers() {
  AsioDrivers drivers;
  std::vector<std::array<char, 64>> names(32);
  std::vector<char*> pointers;
  for (auto& name : names) pointers.push_back(name.data());
  const long count = drivers.getDriverNames(pointers.data(), static_cast<long>(pointers.size()));
  std::cout << "{\"event\":\"drivers\",\"drivers\":[";
  for (long index = 0; index < count; ++index) {
    if (index > 0) std::cout << ',';
    std::cout << "\"" << escape_json(names[index].data()) << "\"";
  }
  std::cout << "]}" << std::endl;
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  const auto options = parse_options(argc, argv);
  if (!options.has_value()) {
    emit("{\"event\":\"error\",\"message\":\"Usage: --list | --driver NAME [--buffer-size N]\"}");
    return 2;
  }
  if (options->list) return list_drivers();
  if (options->driver_name.empty()) {
    emit("{\"event\":\"error\",\"message\":\"ASIO driver name is required\"}");
    return 2;
  }

  SetConsoleCtrlHandler(console_handler, TRUE);
  std::signal(SIGINT, stop_handler);
  g_drivers = new AsioDrivers();
  if (!loadAsioDriver(const_cast<char*>(options->driver_name.c_str()))) {
    emit("{\"event\":\"error\",\"message\":\"Could not open ASIO driver\"}");
    return 1;
  }
  g_engine.info.asioVersion = 2;
  g_engine.info.sysRef = GetConsoleWindow();
  if (ASIOInit(&g_engine.info) != ASE_OK) {
    emit("{\"event\":\"error\",\"message\":\"ASIOInit failed: " + escape_json(g_engine.info.errorMessage) + "\"}");
    cleanup();
    return 1;
  }
  // Only actually change the driver's clock when it isn't already running at
  // the requested rate -- ASIOSetSampleRate on an interface already at that
  // rate can still cause an audible click/resync and can retune other
  // programs sharing the same clock domain, all for no benefit.
  ASIOSampleRate current_rate = 0.0;
  const bool rate_known = ASIOGetSampleRate(&current_rate) == ASE_OK;
  if (options->sample_rate > 0.0 && ASIOCanSampleRate(options->sample_rate) == ASE_OK &&
      (!rate_known || std::abs(current_rate - options->sample_rate) > 0.5))
    ASIOSetSampleRate(options->sample_rate);
  if (!create_buffers(*options) || ASIOStart() != ASE_OK) {
    emit("{\"event\":\"error\",\"message\":\"Could not start ASIO full-duplex stream\"}");
    cleanup();
    return 1;
  }
  ASIOSampleRate rate = 0.0;
  ASIOGetSampleRate(&rate);
  // Query the running driver after OutputReady negotiation, not the requested
  // callback size. A failed query must never be presented as zero latency.
  const bool driver_latency = ASIOGetLatencies(&g_engine.input_latency, &g_engine.output_latency) == ASE_OK &&
                              g_engine.input_latency >= 0 && g_engine.output_latency >= 0;
  if (!driver_latency) g_engine.input_latency = g_engine.output_latency = g_engine.buffer_size;
  std::ostringstream started;
  started << "{\"event\":\"started\",\"driver\":\"" << escape_json(options->driver_name)
          << "\",\"sample_rate\":" << rate << ",\"buffer_size\":" << g_engine.buffer_size
          << ",\"input_latency\":" << g_engine.input_latency << ",\"output_latency\":" << g_engine.output_latency
          << ",\"input_channels\":" << g_engine.input_count
          << ",\"latency_source\":\"" << (driver_latency ? "asio-driver-report" : "asio-buffer-estimate") << "\"}";
  emit(started.str());
  while (g_running) {
    std::ostringstream level;
    level << "{\"event\":\"level\",\"rms_db\":" << g_rms.load()
          << ",\"clipping\":" << (g_clipping.load() ? "true" : "false") << "}";
    emit(level.str());
    Sleep(100);
  }
  ASIOStop();
  cleanup();
  emit(std::string("{\"event\":\"stopped\",\"reset_requested\":") +
       (g_reset_requested.load() ? "true" : "false") + "}");
  return 0;
}
