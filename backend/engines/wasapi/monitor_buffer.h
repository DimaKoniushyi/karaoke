#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace shared_audio {
// IAudioClient3 separates the engine transfer period from the application's
// processing chunk.  For realtime monitoring the engine must always run at
// the smallest period the endpoint advertises; making it follow a larger UI
// block only adds capture/render time and provides no DSP benefit.
inline uint32_t lowest_latency_engine_period(uint32_t minimum, uint32_t maximum, uint32_t fundamental) {
    if (!fundamental || !minimum || minimum > maximum)
        throw std::runtime_error("Invalid shared engine periods");
    const uint64_t aligned = (uint64_t(minimum) + fundamental - 1) / fundamental * fundamental;
    if (aligned > maximum) throw std::runtime_error("No valid shared engine period");
    return static_cast<uint32_t>(aligned);
}
inline bool shorter_engine_period(uint32_t candidate_frames, uint32_t candidate_rate,
                                  uint32_t current_frames, uint32_t current_rate) {
    if (!candidate_frames || !candidate_rate) return false;
    if (!current_frames || !current_rate) return true;
    // Compare exact rational durations without floating-point tie noise.
    return uint64_t(candidate_frames) * current_rate < uint64_t(current_frames) * candidate_rate;
}
inline bool prefer_engine_candidate(uint32_t candidate_frames, uint32_t candidate_rate, bool candidate_raw,
                                    uint32_t current_frames, uint32_t current_rate, bool current_raw) {
    if (shorter_engine_period(candidate_frames, candidate_rate, current_frames, current_rate)) return true;
    if (shorter_engine_period(current_frames, current_rate, candidate_frames, candidate_rate)) return false;
    return candidate_raw && !current_raw;
}
inline uint32_t processing_chunk_size(uint32_t requested, uint32_t capture_period) {
    if (!requested || !capture_period)
        throw std::runtime_error("Invalid processing chunk size");
    return std::max(requested, capture_period);
}
inline uint32_t monitor_queue_target(uint32_t requested, uint32_t capture_period,
                                     bool input_exclusive) {
    if (!requested || !capture_period)
        throw std::runtime_error("Invalid monitor queue target");
    // Exclusive mode changes how the endpoint obtains samples, not how much
    // audio the user-mode bridge must deliberately retain.  Keeping a whole
    // capture packet here adds that packet's duration directly to the audible
    // round trip.  The emergency allocation still absorbs scheduling stalls;
    // drift control targets only the user's small processing block.
    (void)input_exclusive;
    return requested;
}
// Mirrors the ASIO bridge's resolve_buffer_size: the device's own
// min/max/fundamental always wins. A user-requested size outside that range
// is clamped into it (and aligned up to the fundamental granularity) rather
// than rejected -- a buffer setting that happens to be too large for one
// endpoint must not fail the whole monitor with "No supported low-latency
// shared WASAPI configuration" when a smaller, still-valid period is right
// there.
inline uint32_t engine_period(uint32_t requested, uint32_t minimum, uint32_t maximum, uint32_t fundamental) {
    if (!requested || !fundamental || minimum > maximum)
        throw std::runtime_error("Invalid shared engine periods");
    const uint64_t wanted = std::clamp<uint64_t>(requested, minimum, maximum);
    const uint64_t aligned = (wanted + fundamental - 1) / fundamental * fundamental;
    return static_cast<uint32_t>(std::clamp<uint64_t>(aligned, minimum, maximum));
}
inline float decode(const uint8_t* data, unsigned bits, bool floating) {
    if (floating) { float value; std::memcpy(&value, data, 4); return std::isfinite(value) ? value : 0; }
    if (bits == 16) { int16_t value; std::memcpy(&value, data, 2); return value / 32768.0F; }
    if (bits == 32) { int32_t value; std::memcpy(&value, data, 4); return static_cast<float>(value / 2147483648.0); }
    int32_t value = int32_t(data[0]) | (int32_t(data[1]) << 8) | (int32_t(data[2]) << 16);
    if (value & 0x800000) value -= 0x1000000;
    return value / 8388608.0F;
}
inline void encode(uint8_t* data, unsigned bits, bool floating, float input) {
    const float value = std::isfinite(input) ? std::clamp(input, -.985F, .985F) : 0;
    if (floating) { std::memcpy(data, &value, 4); return; }
    if (bits == 16) { const auto pcm = static_cast<int16_t>(value * 32767); std::memcpy(data, &pcm, 2); return; }
    if (bits == 32) { const auto pcm = static_cast<int32_t>(double(value) * 2147483647); std::memcpy(data, &pcm, 4); return; }
    const auto pcm = static_cast<int32_t>(value * 8388607);
    for (unsigned i = 0; i < 3; ++i) data[i] = static_cast<uint8_t>((static_cast<uint32_t>(pcm) >> (i * 8)) & 255);
}
// Single-threaded, preallocated queue. Independent device rates are converted
// without accumulating an unbounded backlog; underruns never repeat old audio.
class MonitorBuffer {
    std::vector<float> samples;
    std::vector<double> timestamps;
    std::vector<double> received_times, processed_times;
    size_t head = 0, used = 0, target_fill = 2;
    uint64_t lost = 0;
    double ratio, base_ratio, phase = 0;
public:
    MonitorBuffer(size_t capacity, double rate_ratio, size_t requested_target_fill = 2) :
        samples(capacity), timestamps(capacity), received_times(capacity), processed_times(capacity),
        target_fill(std::clamp<size_t>(requested_target_fill, 2, capacity - 1)),
        ratio(rate_ratio), base_ratio(rate_ratio) {
        if (capacity < 2 || !std::isfinite(ratio) || ratio <= 0) throw std::runtime_error("Invalid monitor queue");
    }
    size_t size() const { return used; }
    size_t available() const {
        if (ratio == 1) return used;
        if (used < 2) return 0;
        // Interpolation needs a following source sample, even when the byte
        // count alone suggests another output frame would fit.
        const double interpolated = std::ceil((used - 1 - phase) / ratio - 1e-9);
        const double advanced = std::floor((used - phase) / ratio + 1e-9);
        return static_cast<size_t>(std::max(0.0, std::min(interpolated, advanced)));
    }
    uint64_t dropped() const { return lost; }
    // A device-reported capture discontinuity (dropped samples between
    // hardware and driver) makes every sample already queued here -- and the
    // resampler's phase against them -- describe audio from before a gap
    // that never reached us. Stitching new post-gap audio onto that stale
    // queue is worse than the brief silence this produces instead.
    void reset() { head = 0; used = 0; phase = 0; }
    // Genuine long-run clock drift between two independent physical devices
    // (no two "48kHz" clocks are ever exactly identical) is not something a
    // fixed ratio compensates for -- left alone, the queue slowly grows or
    // drains until it either drops samples (a click, see push() below) or
    // underruns. A tiny proportional nudge toward a mid-fill target corrects
    // for it continuously; the correction is capped small enough (0.02%,
    // ratio clamped to +/-0.1% of nominal) to never be audible as pitch
    // wobble on its own. The target is the single safety period supplied by
    // the engine, not half of the emergency allocation: spare capacity is
    // for stalls and must not become deliberate audible latency. Call once
    // per output callback.
    void nudge() {
        const double error = double(used) - double(target_fill);
        const double correction = std::clamp(error / double(samples.size()) * 0.02, -0.0002, 0.0002);
        ratio = std::clamp(base_ratio * (1.0 + correction), base_ratio * 0.999, base_ratio * 1.001);
    }
    void push(const float* input, size_t count, double captured_at = 0, double step = 0,
              double received_at = 0, double processed_at = 0) {
        bool dropped_any = false;
        for (size_t i = 0; i < count; ++i) {
            if (used == samples.size()) { head = (head + 1) % samples.size(); --used; ++lost; dropped_any = true; }
            const size_t index = (head + used++) % samples.size();
            samples[index] = input[i];
            timestamps[index] = captured_at > 0 ? captured_at + i * step : 0;
            received_times[index] = received_at;
            processed_times[index] = processed_at;
        }
        // Reset phase once for the whole burst instead of on every dropped
        // sample within it -- each reset is itself a small interpolation
        // discontinuity, and a sustained overflow could otherwise repeat it
        // count times in a single push() call.
        if (dropped_any) phase = 0;
    }
    bool pop(float& output, double* captured_at = nullptr, double* received_at = nullptr, double* processed_at = nullptr) {
        if (captured_at) *captured_at = 0;
        if (received_at) *received_at = 0;
        if (processed_at) *processed_at = 0;
        if (ratio == 1 && used) {
            output = samples[head];
            if (captured_at) *captured_at = timestamps[head];
            if (received_at) *received_at = received_times[head];
            if (processed_at) *processed_at = processed_times[head];
            head = (head + 1) % samples.size(); --used; return true;
        }
        const size_t consume = static_cast<size_t>(phase + ratio);
        if (used < std::max<size_t>(2, consume)) { output = 0; return false; }
        output = static_cast<float>(samples[head] * (1 - phase) + samples[(head + 1) % samples.size()] * phase);
        const double first = timestamps[head], second = timestamps[(head + 1) % samples.size()];
        if (captured_at && first > 0 && second > 0) *captured_at = first * (1 - phase) + second * phase;
        if (received_at) *received_at = received_times[head] * (1 - phase) + received_times[(head + 1) % samples.size()] * phase;
        if (processed_at) *processed_at = processed_times[head] * (1 - phase) + processed_times[(head + 1) % samples.size()] * phase;
        phase += ratio - consume;
        head = (head + consume) % samples.size();
        used -= consume;
        return true;
    }
};
}
