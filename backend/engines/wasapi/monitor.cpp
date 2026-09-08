// Local shared-mode I/O only. DSP stays in the existing monitor worker.
#define NOMINMAX
#include <windows.h>
#include <audioclient.h>
#include <mmdeviceapi.h>
#include <functiondiscoverykeys_devpkey.h>
#include <ksmedia.h>
#include <avrt.h>
#include <wrl/client.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include "monitor_buffer.h"

using Microsoft::WRL::ComPtr;
using Process = int (__cdecl *)(const float*, float*, uint32_t);
struct Info {
    uint32_t sample_rate, output_sample_rate, blocksize, input_period, output_period;
    uint32_t input_buffer, output_buffer;
    double input_latency_ms, output_latency_ms;
    // Whether AUDCLNT_STREAMOPTIONS_RAW actually won for each endpoint (see
    // Endpoint::open's try_candidate) -- some capture/render drivers reject
    // RAW and this silently falls back to a non-RAW candidate, which leaves
    // Windows APOs (loudness/AGC/noise-suppression "enhancements") back in
    // the path and can add their own latency. Previously invisible: the
    // engine still opened and ran normally either way.
    uint32_t input_raw, output_raw;
};
struct Statistics {
    uint64_t captured_frames = 0, rendered_frames = 0, dropped_frames = 0;
    uint64_t underruns = 0, discontinuities = 0, queued_frames = 0;
    double stream_latency_ms = -1;
    double capture_delivery_ms = -1, program_residence_ms = -1, queue_residence_ms = -1;
    double output_clock_lead_ms = -1, render_submit_ms = 0, render_padding_ms = 0;
    double capture_processing_ms = 0, event_wait_ms = 0, pump_gap_ms = 0;
};
static double monotonic_seconds() {
    static const double frequency = [] { LARGE_INTEGER value; QueryPerformanceFrequency(&value); return double(value.QuadPart); }();
    LARGE_INTEGER value;
    QueryPerformanceCounter(&value);
    return double(value.QuadPart) / frequency;
}
static void check(HRESULT hr, const char* operation) {
    if (SUCCEEDED(hr)) return;
    std::ostringstream out;
    out << operation << ": HRESULT 0x" << std::hex << static_cast<uint32_t>(hr);
    throw std::runtime_error(out.str());
}
static void error_text(char* target, uint32_t size, const std::exception& error) {
    if (target && size) { strncpy_s(target, size, error.what(), _TRUNCATE); }
}
struct Apartment {
    bool initialized = false;
    Apartment() {
        const HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        // PortAudio may already have initialized this worker thread as STA.
        // We keep its apartment and do not uninitialize somebody else's COM.
        if (hr != RPC_E_CHANGED_MODE) { check(hr, "CoInitializeEx"); initialized = true; }
    }
    ~Apartment() { if (initialized) CoUninitialize(); }
};
struct Handle {
    HANDLE value = nullptr;
    ~Handle() { if (value) CloseHandle(value); }
};
static std::wstring name_of(IMMDevice* device) {
    ComPtr<IPropertyStore> store;
    check(device->OpenPropertyStore(STGM_READ, &store), "OpenPropertyStore");
    PROPVARIANT value{};
    const HRESULT hr = store->GetValue(PKEY_Device_FriendlyName, &value);
    std::wstring name = SUCCEEDED(hr) && value.vt == VT_LPWSTR ? value.pwszVal : L"";
    PropVariantClear(&value);
    check(hr, "Get endpoint name");
    return name;
}
static ComPtr<IMMDevice> find_device(IMMDeviceEnumerator* enumerator, EDataFlow flow, const wchar_t* name) {
    ComPtr<IMMDevice> result;
    if (!name || !*name) {
        check(enumerator->GetDefaultAudioEndpoint(flow, eConsole, &result), "Get default endpoint");
        return result;
    }
    ComPtr<IMMDeviceCollection> devices;
    check(enumerator->EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE, &devices), "EnumAudioEndpoints");
    UINT count = 0;
    check(devices->GetCount(&count), "Get endpoint count");
    for (UINT index = 0; index < count; ++index) {
        ComPtr<IMMDevice> item;
        check(devices->Item(index, &item), "Get endpoint");
        if (name_of(item.Get()) != name) continue;
        if (result) throw std::runtime_error("Selected audio endpoint name is ambiguous");
        result = item;
    }
    if (!result) throw std::runtime_error("Selected audio endpoint is unavailable; no default-device substitution");
    return result;
}
struct Endpoint {
    ComPtr<IAudioClient3> client;
    WAVEFORMATEX* format = nullptr;
    Handle event;
    UINT32 period = 0, buffer = 0;
    bool started = false, floating = false, raw = false;
    ~Endpoint() {
        if (started) client->Stop();
        client.Reset();
        if (format) CoTaskMemFree(format);
    }
    void open(IMMDeviceEnumerator* enumerator, EDataFlow flow, const wchar_t* name, uint32_t requested, bool initialize) {
        auto device = find_device(enumerator, flow, name);
        auto valid_format = [](WAVEFORMATEX* value, bool& is_float) {
            WORD tag = value->wFormatTag;
            if (tag == WAVE_FORMAT_EXTENSIBLE && value->cbSize >= 22) {
                auto* ext = reinterpret_cast<WAVEFORMATEXTENSIBLE*>(value);
                if (ext->SubFormat == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT) tag = WAVE_FORMAT_IEEE_FLOAT;
                else if (ext->SubFormat == KSDATAFORMAT_SUBTYPE_PCM) tag = WAVE_FORMAT_PCM;
            }
            is_float = tag == WAVE_FORMAT_IEEE_FLOAT && value->wBitsPerSample == 32;
            const bool pcm = tag == WAVE_FORMAT_PCM &&
                (value->wBitsPerSample == 16 || value->wBitsPerSample == 24 || value->wBitsPerSample == 32);
            return (is_float || pcm) && value->nChannels && value->nSamplesPerSec &&
                value->nBlockAlign == value->nChannels * (value->wBitsPerSample / 8);
        };
        auto try_candidate = [&](AUDIO_STREAM_CATEGORY category, AUDCLNT_STREAMOPTIONS options) {
            ComPtr<IAudioClient3> candidate;
            if (FAILED(device->Activate(__uuidof(IAudioClient3), CLSCTX_ALL, nullptr,
                                        reinterpret_cast<void**>(candidate.GetAddressOf())))) return false;
            AudioClientProperties properties{};
            properties.cbSize = sizeof(properties);
            properties.eCategory = category;
            properties.Options = options;
            if (FAILED(candidate->SetClientProperties(&properties))) return false;
            WAVEFORMATEX* candidate_format = nullptr;
            if (FAILED(candidate->GetMixFormat(&candidate_format))) return false;
            bool candidate_floating = false;
            if (!valid_format(candidate_format, candidate_floating)) {
                CoTaskMemFree(candidate_format);
                return false;
            }
            UINT32 normal = 0, fundamental = 0, minimum = 0, maximum = 0;
            if (FAILED(candidate->GetSharedModeEnginePeriod(
                    candidate_format, &normal, &fundamental, &minimum, &maximum))) {
                CoTaskMemFree(candidate_format);
                return false;
            }
            UINT32 candidate_period = 0;
            try {
                candidate_period = shared_audio::engine_period(requested, minimum, maximum, fundamental);
            } catch (const std::exception&) {
                CoTaskMemFree(candidate_format);
                return false;
            }
            if (client && candidate_period >= period) {
                CoTaskMemFree(candidate_format);
                return true;
            }
            if (format) CoTaskMemFree(format);
            client = candidate;
            format = candidate_format;
            floating = candidate_floating;
            period = candidate_period;
            raw = options == AUDCLNT_STREAMOPTIONS_RAW;
            return true;
        };
        // Windows permits different AUDIO_STREAM_CATEGORY sets for capture
        // and render. Probe only categories valid for this endpoint and select
        // the shortest period reported by its driver. RAW bypasses endpoint
        // APOs that can add monitoring latency. This remains shared mode.
        if (flow == eCapture) {
            // Prefer neutral Other on a tie. Speech/Communications win only
            // when the endpoint genuinely exposes a shorter period for them.
            try_candidate(AudioCategory_Other, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_Speech, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_Communications, AUDCLNT_STREAMOPTIONS_RAW);
            // Some capture drivers reject RAW. Other is the neutral fallback
            // and avoids opting into communications signal processing.
            if (!client)
                try_candidate(AudioCategory_Other, static_cast<AUDCLNT_STREAMOPTIONS>(0));
        } else {
            try_candidate(AudioCategory_Media, AUDCLNT_STREAMOPTIONS_RAW);
            // GameChat is the real-time render category that explicitly
            // does not attenuate other streams. Some consumer drivers offer
            // it a shorter shared period than Media; it only wins below when
            // that is genuinely true, so equal-period devices retain Media.
            try_candidate(AudioCategory_GameChat, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_Movie, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_SoundEffects, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_GameEffects, AUDCLNT_STREAMOPTIONS_RAW);
            // Media keeps the backing track at normal playback priority.
            if (!client)
                try_candidate(AudioCategory_Media, static_cast<AUDCLNT_STREAMOPTIONS>(0));
        }
        if (!client) throw std::runtime_error("No supported low-latency shared WASAPI configuration");
        if (!initialize) return;
        check(client->InitializeSharedAudioStream(AUDCLNT_STREAMFLAGS_EVENTCALLBACK, period, format, nullptr), "InitializeSharedAudioStream");
        event.value = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!event.value) throw std::runtime_error("CreateEvent failed");
        check(client->SetEventHandle(event.value), "SetEventHandle");
        check(client->GetBufferSize(&buffer), "GetBufferSize");
    }
    double latency() {
        REFERENCE_TIME value = 0;
        return SUCCEEDED(client->GetStreamLatency(&value)) ? value / 10000.0 : -1;
    }
    void start() { check(client->Start(), "Start shared stream"); started = true; }
    float read(const BYTE* frame) const {
        // Some capture endpoints put a mono microphone's actual signal on a
        // channel other than 0 (e.g. Right) even while still negotiating a
        // stereo mix format -- reading only channel 0 made those mics appear
        // silent despite being selected and opened successfully. Take
        // whichever channel in the frame carries the strongest sample.
        const unsigned bytes = format->wBitsPerSample / 8;
        float best = 0.0F, best_magnitude = -1.0F;
        for (unsigned channel = 0; channel < format->nChannels; ++channel) {
            const float value = shared_audio::decode(frame + channel * bytes, format->wBitsPerSample, floating);
            const float magnitude = std::abs(value);
            if (magnitude > best_magnitude) { best = value; best_magnitude = magnitude; }
        }
        return best;
    }
    void write(BYTE* data, float sample) const {
        const unsigned bytes = format->wBitsPerSample / 8;
        for (unsigned channel = 0; channel < format->nChannels; ++channel)
            shared_audio::encode(data + channel * bytes, format->wBitsPerSample, floating, channel < 2 ? sample : 0);
    }
};
struct Engine {
    Apartment apartment; // Last member destroyed, after all COM interfaces.
    Endpoint input, output;
    ComPtr<IAudioCaptureClient> capture;
    ComPtr<IAudioRenderClient> render;
    ComPtr<IAudioClock> clock;
    UINT64 clock_frequency = 0, written_frames = 0;
    std::unique_ptr<shared_audio::MonitorBuffer> queue;
    std::vector<float> source, processed;
    Statistics stats;
    Process process = nullptr;
    HANDLE scheduling = nullptr;
    double pump_finished = 0;
    // Live-updatable (wm_set_gain) so a volume-slider change applies to the
    // native raw pass-through the same way it already does to the Python DSP
    // path -- relaxed ordering for the same reason as raw_active below.
    std::atomic<float> gain{1.0f};
    // Set by wm_set_raw, read from pump(). A momentary "listen to the raw
    // voice" check: skip the Python callback entirely and just apply gain
    // in native code, so the round trip never crosses into the interpreter
    // at all for that block. Only ever armed by monitor_worker.py when it
    // knows nothing downstream (the relay) depends on that callback running
    // -- see set_raw's caller. relaxed ordering is enough: this is read once
    // per block on the audio thread and only ever toggled by a single writer
    // thread, with no other state that must be seen consistently with it.
    std::atomic<bool> raw_active{false};
    ~Engine() { if (scheduling) AvRevertMmThreadCharacteristics(scheduling); }
    void open(const wchar_t* input_name, const wchar_t* output_name, uint32_t blocksize, float requested_gain,
              Info& info, bool initialize) {
        gain = requested_gain;
        if (!blocksize || blocksize > 8192) throw std::runtime_error("Invalid fixed processing buffer");
        ComPtr<IMMDeviceEnumerator> enumerator;
        check(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, IID_PPV_ARGS(&enumerator)), "Create enumerator");
        input.open(enumerator.Get(), eCapture, input_name, blocksize, initialize);
        output.open(enumerator.Get(), eRender, output_name, blocksize, initialize);
        info = {input.format->nSamplesPerSec, output.format->nSamplesPerSec, blocksize, input.period, output.period,
                input.buffer, output.buffer, initialize ? input.latency() : -1, initialize ? output.latency() : -1,
                input.raw ? 1u : 0u, output.raw ? 1u : 0u};
        if (!initialize) return;
        check(input.client->GetService(IID_PPV_ARGS(&capture)), "Get capture service");
        check(output.client->GetService(IID_PPV_ARGS(&render)), "Get render service");
        if (FAILED(output.client->GetService(IID_PPV_ARGS(&clock))) ||
            FAILED(clock->GetFrequency(&clock_frequency)) || !clock_frequency) clock.Reset();
        const double ratio = double(info.sample_rate) / info.output_sample_rate;
        const size_t capacity = 2 * std::max(input.period, UINT32(std::ceil(output.period * ratio))) + blocksize * 2;
        // The driver period is allocation/callback cadence, not a latency
        // target. On consumer drivers it is commonly 10ms even when the user
        // selected 32/64 frames. Steering clock-drift correction toward a
        // whole period therefore manufactures that much queued delay inside
        // the app. Retain only the requested processing block (or less when
        // the endpoint period itself is shorter); spare capacity still
        // absorbs scheduling stalls without becoming intentional latency.
        const size_t safety_frames = std::max<size_t>(2, std::min<size_t>(blocksize,
            size_t(std::ceil(output.period * ratio))));
        queue = std::make_unique<shared_audio::MonitorBuffer>(capacity, ratio, safety_frames);
        source.resize(blocksize);
        processed.resize(blocksize);
    }
    void start(Process callback) {
        process = callback;
        DWORD task = 0;
        scheduling = AvSetMmThreadCharacteristicsW(L"Pro Audio", &task);
        // Registration selects the MMCSS task profile, but does not select
        // its highest realtime priority by itself. Keep only this pump thread
        // critical so renderer/AI load cannot make it miss an endpoint event.
        if (scheduling) AvSetMmThreadPriority(scheduling, AVRT_PRIORITY_CRITICAL);
        // Start playback with the first real packet, not a period of silence
        // queued ahead of the microphone. Capture alone drives startup events.
        input.start();
    }
    void pump(uint32_t timeout) {
        const double entering = monotonic_seconds();
        stats.pump_gap_ms = pump_finished ? (entering - pump_finished) * 1000 : 0;
        HANDLE events[] = {input.event.value, output.event.value};
        if (WaitForMultipleObjects(2, events, FALSE, timeout) == WAIT_FAILED)
            throw std::runtime_error("Audio event wait failed");
        stats.event_wait_ms = (monotonic_seconds() - entering) * 1000;
        // A ready output block must not wait behind another capture/DSP burst.
        render_ready();
        UINT32 available = 0;
        check(capture->GetNextPacketSize(&available), "GetNextPacketSize");
        // Drain capture after either event, handing each completed packet to
        // playback before processing the next queued packet.
        for (unsigned packet = 0; available && packet < 32; ++packet) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            UINT64 captured_qpc = 0;
            check(capture->GetBuffer(&data, &frames, &flags, nullptr, &captured_qpc), "Capture GetBuffer");
            const double received_at = monotonic_seconds();
            if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) {
                ++stats.discontinuities;
                // The samples already queued (and the resampler's phase
                // against them) describe audio from before whatever gap the
                // driver just reported -- stitching new post-gap audio onto
                // them would keep the output timeline continuous but wrong.
                queue->reset();
            }
            bool ok = true;
            const bool raw = raw_active.load(std::memory_order_relaxed);
            for (uint32_t offset = 0; offset < frames; ) {
                const uint32_t count = std::min<uint32_t>(frames - offset, uint32_t(source.size()));
                for (uint32_t index = 0; index < count; ++index)
                    source[index] = flags & AUDCLNT_BUFFERFLAGS_SILENT ? 0 : input.read(data + (offset + index) * input.format->nBlockAlign);
                if (raw) {
                    // Native-only pass-through: no Python callback this
                    // block at all, just gain and a hard clip, matching the
                    // same clamp the Python "dry_monitor" bypass applies.
                    const float current_gain = gain.load(std::memory_order_relaxed);
                    for (uint32_t index = 0; index < count; ++index)
                        processed[index] = std::clamp(source[index] * current_gain, -1.0f, 1.0f);
                } else if (!process(source.data(), processed.data(), count)) {
                    ok = false;
                    break;
                }
                const double timestamp = flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR ? 0 : captured_qpc * 1e-7;
                queue->push(processed.data(), count, timestamp > 0 ? timestamp + double(offset) / input.format->nSamplesPerSec : 0,
                            1.0 / input.format->nSamplesPerSec, received_at, monotonic_seconds());
                offset += count;
            }
            check(capture->ReleaseBuffer(frames), "Capture ReleaseBuffer");
            stats.capture_processing_ms = (monotonic_seconds() - received_at) * 1000;
            if (!ok) throw std::runtime_error("Microphone processing callback failed");
            stats.captured_frames += frames;
            render_ready();
            check(capture->GetNextPacketSize(&available), "GetNextPacketSize");
        }
        pump_finished = monotonic_seconds();
    }
    void render_ready() {
        UINT32 padding = 0;
        check(output.client->GetCurrentPadding(&padding), "GetCurrentPadding");
        stats.render_padding_ms = double(padding) * 1000 / output.format->nSamplesPerSec;
        queue->nudge();
        // Allocation capacity is NOT a target queue depth. Submit at most one
        // engine period instead of filling the entire Windows render buffer.
        const UINT32 target = std::min(output.period, output.buffer);
        // An early render event must not enqueue a period of silence ahead of
        // microphone data that arrives a moment later. Submit only ready audio.
        const auto ready = UINT32(queue->available());
        const UINT32 count = padding < target ? std::min(target - padding, ready) : 0;
        // Windows wants audio and the queue has none at all -- the block
        // below (which is where underruns were counted) never runs in this
        // case, so a fully-starved queue was previously invisible in the
        // stats even though it is the more severe starvation than a partial
        // shortfall.
        if (padding < target && !count) ++stats.underruns;
        if (count) {
            double presentation = 0;
            UINT64 position = 0, qpc = 0;
            if (output.started && clock && clock->GetPosition(&position, &qpc) == S_OK && position && qpc) {
                const double played = double(position) / clock_frequency;
                // After an underrun the clock keeps advancing through silence.
                if (!padding) written_frames = std::max(written_frames, UINT64(played * output.format->nSamplesPerSec));
                presentation = qpc * 1e-7 + double(written_frames) / output.format->nSamplesPerSec - played;
            }
            BYTE* data = nullptr;
            const double submit_started = monotonic_seconds();
            check(render->GetBuffer(count, &data), "Render GetBuffer");
            bool starved = false;
            double transit = 0;
            double delivery_sum = 0, received_sum = 0, processed_sum = 0;
            unsigned timestamped = 0, delivered = 0, received = 0;
            for (UINT32 index = 0; index < count; ++index) {
                float sample = 0;
                double captured_at = 0, received_at = 0, processed_at = 0;
                if (!queue->pop(sample, &captured_at, &received_at, &processed_at)) starved = true;
                output.write(data + index * output.format->nBlockAlign, sample);
                // Program timings remain measurable even when a device does
                // not provide valid capture timestamps or a playback clock.
                if (received_at > 0 && processed_at >= received_at) {
                    ++received; received_sum += received_at; processed_sum += processed_at;
                    if (captured_at > 0 && received_at >= captured_at) {
                        ++delivered; delivery_sum += received_at - captured_at;
                    }
                }
                const double age = presentation + double(index) / output.format->nSamplesPerSec - captured_at;
                if (captured_at > 0 && presentation > 0 && age >= 0 && age < 1) {
                    transit += age; ++timestamped;
                }
            }
            check(render->ReleaseBuffer(count, 0), "Render ReleaseBuffer");
            if (!output.started) output.start();
            const double submitted = monotonic_seconds();
            stats.render_submit_ms = (submitted - submit_started) * 1000;
            stats.capture_delivery_ms = delivered ? delivery_sum * 1000 / delivered : -1;
            stats.program_residence_ms = received ? (submitted - received_sum / received) * 1000 : -1;
            stats.queue_residence_ms = received ? (submitted - processed_sum / received) * 1000 : -1;
            stats.output_clock_lead_ms = presentation > 0
                ? (presentation + double(count - 1) / (2 * output.format->nSamplesPerSec) - submitted) * 1000 : -1;
            if (starved) ++stats.underruns;
            stats.rendered_frames += count;
            written_frames += count;
            stats.stream_latency_ms = timestamped ? transit * 1000 / timestamped : -1;
        }
        stats.dropped_frames = queue->dropped();
        stats.queued_frames = queue->size();
    }
};

#define API extern "C" __declspec(dllexport)
// Bump when exported structures change; prevent mixed DLL/Python layouts.
API uint32_t __cdecl wm_abi_version() { return 4; }
API void* __cdecl wm_open(const wchar_t* input, const wchar_t* output, uint32_t blocksize, float gain, Info* info, char* error, uint32_t size) {
    try {
        auto engine = std::make_unique<Engine>();
        engine->open(input, output, blocksize, gain, *info, true);
        return engine.release();
    } catch (const std::exception& failure) { error_text(error, size, failure); return nullptr; }
}
API int __cdecl wm_probe(const wchar_t* input, const wchar_t* output, uint32_t blocksize, float gain, Info* info, char* error, uint32_t size) {
    try { Engine engine; engine.open(input, output, blocksize, gain, *info, false); return 1; }
    catch (const std::exception& failure) { error_text(error, size, failure); return 0; }
}
API int __cdecl wm_start(void* handle, Process callback, char* error, uint32_t size) {
    try { static_cast<Engine*>(handle)->start(callback); return 1; }
    catch (const std::exception& failure) { error_text(error, size, failure); return 0; }
}
API int __cdecl wm_pump(void* handle, uint32_t timeout, Statistics* stats, char* error, uint32_t size) {
    try { auto* engine = static_cast<Engine*>(handle); engine->pump(timeout); *stats = engine->stats; return 1; }
    catch (const std::exception& failure) { error_text(error, size, failure); return 0; }
}
// Toggled from monitor_worker.py's stdin live-update reader thread, read on
// the realtime audio thread inside pump() -- see Engine::raw_active. No
// error path: setting a bool on a live engine cannot fail.
API void __cdecl wm_set_raw(void* handle, int raw) {
    if (handle) static_cast<Engine*>(handle)->raw_active.store(raw != 0, std::memory_order_relaxed);
}
// Mirrors wm_set_raw -- a live volume-slider change must reach the native
// raw pass-through the same way it already reaches the Python DSP path.
API void __cdecl wm_set_gain(void* handle, float gain) {
    if (handle) static_cast<Engine*>(handle)->gain.store(gain, std::memory_order_relaxed);
}
API void __cdecl wm_close(void* handle) { delete static_cast<Engine*>(handle); }
