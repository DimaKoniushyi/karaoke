// Event-driven local I/O. Render remains shared; capture may be exclusive.
#define NOMINMAX
#include <windows.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <mmdeviceapi.h>
#include <functiondiscoverykeys_devpkey.h>
#include <ksmedia.h>
#include <avrt.h>
#include <wrl/client.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cwctype>
#include <cstring>
#include <iterator>
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
    uint32_t input_min_period, output_min_period;
    uint32_t input_period_locked, output_period_locked;
    uint32_t input_exclusive, output_exclusive;
};
struct Statistics {
    uint64_t captured_frames = 0, rendered_frames = 0, dropped_frames = 0;
    uint64_t underruns = 0, discontinuities = 0, queued_frames = 0;
    double stream_latency_ms = -1;
    double capture_delivery_ms = -1, program_residence_ms = -1, queue_residence_ms = -1;
    double output_clock_lead_ms = -1, render_submit_ms = 0, render_padding_ms = 0;
    double capture_processing_ms = 0, event_wait_ms = 0, pump_gap_ms = 0;
    double resample_ratio = -1;
};
static double monotonic_seconds() {
    static const double frequency = [] { LARGE_INTEGER value; QueryPerformanceFrequency(&value); return double(value.QuadPart); }();
    LARGE_INTEGER value;
    QueryPerformanceCounter(&value);
    return double(value.QuadPart) / frequency;
}
// Diagnostic-only endpoint phase scan.  A value is never supplied by the
// product; backend/tools/probe_wasapi.py may set it in a short-lived child
// process while output is forced to silence.  Keep it bounded so a malformed
// environment cannot stall normal audio startup.
static uint32_t capture_start_delay_us(uint32_t automatic_delay_us) {
    wchar_t text[32]{};
    const DWORD length = GetEnvironmentVariableW(
        L"ADVOICE_WASAPI_CAPTURE_START_DELAY_US", text, DWORD(std::size(text)));
    if (!length) return automatic_delay_us;
    if (length >= std::size(text)) return automatic_delay_us;
    wchar_t* end = nullptr;
    const unsigned long parsed = std::wcstoul(text, &end, 10);
    if (end == text || *end != L'\0') return automatic_delay_us;
    const uint32_t requested = parsed > UINT32_MAX ? UINT32_MAX : uint32_t(parsed);
    return std::min<uint32_t>(requested, 20000);
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
static void wait_capture_start_phase(uint32_t microseconds) {
    if (!microseconds) return;
    // CREATE_WAITABLE_TIMER_HIGH_RESOLUTION is 0x2 on supported Windows 10+
    // SDKs. Use the numeric flag so older build-tool headers can still build
    // this diagnostic, then fall back to an ordinary waitable timer.
    Handle timer;
    timer.value = CreateWaitableTimerExW(nullptr, nullptr, 0x2, TIMER_ALL_ACCESS);
    if (!timer.value) timer.value = CreateWaitableTimerW(nullptr, TRUE, nullptr);
    if (!timer.value) return;
    LARGE_INTEGER due{};
    due.QuadPart = -static_cast<LONGLONG>(microseconds) * 10;
    if (SetWaitableTimer(timer.value, &due, 0, nullptr, nullptr, FALSE))
        WaitForSingleObject(timer.value, microseconds / 1000 + 50);
}
static std::wstring property_of(IMMDevice* device, const PROPERTYKEY& key) {
    ComPtr<IPropertyStore> store;
    check(device->OpenPropertyStore(STGM_READ, &store), "OpenPropertyStore");
    PROPVARIANT value{};
    const HRESULT hr = store->GetValue(key, &value);
    std::wstring name = SUCCEEDED(hr) && value.vt == VT_LPWSTR ? value.pwszVal : L"";
    PropVariantClear(&value);
    check(hr, "Get endpoint property");
    return name;
}
// Interface/product label used by the Windows audio endpoint property store.
static const PROPERTYKEY kAudioInterfaceName = {
    {0xb3f8fa53, 0x0004, 0x438e, {0x90, 0x03, 0x51, 0xa4, 0x6e, 0x13, 0x9b, 0xfc}}, 6
};
static std::wstring name_of(IMMDevice* device) {
    auto name = property_of(device, PKEY_Device_FriendlyName);
    return name.empty() ? property_of(device, PKEY_Device_DeviceDesc) : name;
}
static bool endpoint_name_matches(IMMDevice* device, const wchar_t* requested) {
    auto lowercase = [](std::wstring value) {
        std::transform(value.begin(), value.end(), value.begin(), towlower);
        return value;
    };
    const auto friendly = lowercase(name_of(device));
    const auto wanted = lowercase(requested ? requested : L"");
    if (friendly == wanted) return true;
    auto without_instance_ordinal = [](std::wstring value) {
        const auto open = value.find(L'(');
        if (open == std::wstring::npos) return value;
        auto cursor = open + 1;
        while (cursor < value.size() && iswdigit(value[cursor])) ++cursor;
        if (cursor > open + 1 && cursor + 1 < value.size()
            && value[cursor] == L'-' && value[cursor + 1] == L' ')
            value.erase(open + 1, cursor + 1 - open);
        return value;
    };
    if (without_instance_ordinal(friendly) == without_instance_ordinal(wanted))
        return true;
    const auto description = lowercase(property_of(device, kAudioInterfaceName));
    // PortAudio decorates the MMDevice friendly name with interface text and
    // sometimes a Windows instance ordinal: "Microphone (2- Realtek Audio)".
    // MMDevice exposes those as two properties. Compare both rather than
    // silently replacing the selected endpoint with a default device.
    return !friendly.empty() && !description.empty() && wanted.starts_with(friendly)
        && wanted.find(description, friendly.size()) != std::wstring::npos;
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
        if (!endpoint_name_matches(item.Get(), name)) continue;
        if (result) throw std::runtime_error("Selected audio endpoint name is ambiguous");
        result = item;
    }
    if (!result) throw std::runtime_error(
        flow == eCapture
            ? "Selected capture endpoint is unavailable; no default-device substitution"
            : "Selected render endpoint is unavailable; no default-device substitution");
    return result;
}
struct Endpoint {
    ComPtr<IAudioClient3> client;
    WAVEFORMATEX* format = nullptr;
    Handle event;
    UINT32 period = 0, minimum_period = 0, buffer = 0;
    bool started = false, floating = false, raw = false, period_locked = false;
    bool exclusive = false;
    AUDIO_STREAM_CATEGORY category = AudioCategory_Other;
    ~Endpoint() {
        if (started) client->Stop();
        client.Reset();
        if (format) CoTaskMemFree(format);
    }
    void open(IMMDeviceEnumerator* enumerator, EDataFlow flow, const wchar_t* name,
              uint32_t requested, bool initialize, bool request_exclusive = false) {
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
        if (request_exclusive) {
            if (flow != eCapture)
                throw std::runtime_error("Exclusive mode is permitted only for microphone capture");
            check(device->Activate(__uuidof(IAudioClient3), CLSCTX_ALL, nullptr,
                                   reinterpret_cast<void**>(client.GetAddressOf())),
                  "Activate exclusive capture");
            check(client->GetMixFormat(&format), "Get exclusive capture format");
            auto adopt_format = [&](const WAVEFORMATEX* candidate) {
                if (client->IsFormatSupported(
                        AUDCLNT_SHAREMODE_EXCLUSIVE, candidate, nullptr) != S_OK)
                    return false;
                const size_t bytes = sizeof(WAVEFORMATEX) + candidate->cbSize;
                auto* accepted = static_cast<WAVEFORMATEX*>(CoTaskMemAlloc(bytes));
                if (!accepted) throw std::bad_alloc();
                std::memcpy(accepted, candidate, bytes);
                CoTaskMemFree(format);
                format = accepted;
                return valid_format(format, floating);
            };
            bool accepted = valid_format(format, floating) && adopt_format(format);
            const UINT32 rates[] = {format->nSamplesPerSec, 48000, 44100, 96000};
            for (const auto rate : rates) {
                if (accepted) break;
                for (const WORD bits : {WORD(32), WORD(24), WORD(16)}) {
                    WAVEFORMATEXTENSIBLE candidate{};
                    candidate.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
                    candidate.Format.nChannels = format->nChannels;
                    candidate.Format.nSamplesPerSec = rate;
                    candidate.Format.wBitsPerSample = bits;
                    candidate.Format.nBlockAlign = WORD(candidate.Format.nChannels * bits / 8);
                    candidate.Format.nAvgBytesPerSec = rate * candidate.Format.nBlockAlign;
                    candidate.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
                    candidate.Samples.wValidBitsPerSample = bits;
                    candidate.dwChannelMask = candidate.Format.nChannels == 1
                        ? SPEAKER_FRONT_CENTER
                        : SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT;
                    candidate.SubFormat = KSDATAFORMAT_SUBTYPE_PCM;
                    if (adopt_format(&candidate.Format)) { accepted = true; break; }
                }
                if (accepted) break;
                WAVEFORMATEXTENSIBLE candidate{};
                candidate.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
                candidate.Format.nChannels = format->nChannels;
                candidate.Format.nSamplesPerSec = rate;
                candidate.Format.wBitsPerSample = 32;
                candidate.Format.nBlockAlign = WORD(candidate.Format.nChannels * 4);
                candidate.Format.nAvgBytesPerSec = rate * candidate.Format.nBlockAlign;
                candidate.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
                candidate.Samples.wValidBitsPerSample = 32;
                candidate.dwChannelMask = candidate.Format.nChannels == 1
                    ? SPEAKER_FRONT_CENTER
                    : SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT;
                candidate.SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
                accepted = adopt_format(&candidate.Format);
            }
            if (!accepted)
                throw std::runtime_error("Selected microphone exposes no supported exclusive PCM/float format");
            REFERENCE_TIME default_period = 0, minimum = 0;
            check(client->GetDevicePeriod(&default_period, &minimum), "Get exclusive capture period");
            minimum_period = std::max<UINT32>(1, UINT32(std::ceil(
                double(minimum) * format->nSamplesPerSec / 10000000.0)));
            period = std::max(requested, minimum_period);
            if (initialize) {
                auto duration = REFERENCE_TIME(std::ceil(
                    double(period) * 10000000.0 / format->nSamplesPerSec));
                HRESULT hr = client->Initialize(
                    AUDCLNT_SHAREMODE_EXCLUSIVE, AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                    duration, duration, format, nullptr);
                if (hr == AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED) {
                    UINT32 aligned = 0;
                    check(client->GetBufferSize(&aligned), "Get aligned exclusive capture buffer");
                    client.Reset();
                    check(device->Activate(__uuidof(IAudioClient3), CLSCTX_ALL, nullptr,
                                           reinterpret_cast<void**>(client.GetAddressOf())),
                          "Reactivate aligned exclusive capture");
                    duration = REFERENCE_TIME(std::ceil(
                        double(aligned) * 10000000.0 / format->nSamplesPerSec));
                    hr = client->Initialize(
                        AUDCLNT_SHAREMODE_EXCLUSIVE, AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                        duration, duration, format, nullptr);
                }
                check(hr, "Initialize exclusive capture");
            }
            exclusive = true;
            raw = true;  // Exclusive capture bypasses the shared audio-engine APO path.
            if (!initialize) return;
            event.value = CreateEventW(nullptr, FALSE, FALSE, nullptr);
            if (!event.value) throw std::runtime_error("CreateEvent failed");
            check(client->SetEventHandle(event.value), "Set exclusive capture event");
            check(client->GetBufferSize(&buffer), "Get exclusive capture buffer");
            period = buffer;
            return;
        }
        struct Candidate {
            ComPtr<IAudioClient3> client;
            WAVEFORMATEX* format = nullptr;
            UINT32 period = 0;
            bool floating = false, raw = false;
            AUDIO_STREAM_CATEGORY category = AudioCategory_Other;
            AUDCLNT_STREAMOPTIONS options = static_cast<AUDCLNT_STREAMOPTIONS>(0);
            ~Candidate() { if (format) CoTaskMemFree(format); }
        };
        std::vector<std::unique_ptr<Candidate>> candidates;
        auto try_candidate = [&](AUDIO_STREAM_CATEGORY category, AUDCLNT_STREAMOPTIONS options) {
            auto candidate = std::make_unique<Candidate>();
            if (FAILED(device->Activate(__uuidof(IAudioClient3), CLSCTX_ALL, nullptr,
                                        reinterpret_cast<void**>(candidate->client.GetAddressOf())))) return false;
            AudioClientProperties properties{};
            properties.cbSize = sizeof(properties);
            properties.eCategory = category;
            properties.Options = options;
            if (FAILED(candidate->client->SetClientProperties(&properties))) return false;
            if (FAILED(candidate->client->GetMixFormat(&candidate->format))) return false;
            if (!valid_format(candidate->format, candidate->floating)) {
                return false;
            }
            UINT32 normal = 0, fundamental = 0, minimum = 0, maximum = 0;
            if (FAILED(candidate->client->GetSharedModeEnginePeriod(
                    candidate->format, &normal, &fundamental, &minimum, &maximum))) {
                return false;
            }
            try {
                candidate->period = shared_audio::lowest_latency_engine_period(minimum, maximum, fundamental);
            } catch (const std::exception&) {
                return false;
            }
            candidate->raw = options == AUDCLNT_STREAMOPTIONS_RAW;
            candidate->category = category;
            candidate->options = options;
            candidates.push_back(std::move(candidate));
            return true;
        };
        // Windows permits different AUDIO_STREAM_CATEGORY sets for capture
        // and render. Probe only categories valid for this endpoint and select
        // the shortest period actually reported by its driver across RAW and
        // normal shared modes. RAW only breaks an equal-period tie: preferring
        // it unconditionally can hide a lower-latency normal shared path on
        // consumer Realtek endpoints. This remains shared in either mode.
        if (flow == eCapture) {
            // Monitoring is bidirectional realtime voice. Communications is
            // the Windows category that selects the endpoint's matching mode
            // and latency policy, so it wins equal-period ties. A genuinely
            // shorter Speech/Other period still wins in stable_sort below.
            try_candidate(AudioCategory_Communications, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_Speech, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_Other, AUDCLNT_STREAMOPTIONS_RAW);
            // Probe normal mode too. It may advertise a genuinely shorter
            // engine quantum than RAW on consumer endpoints.
            try_candidate(AudioCategory_Communications, static_cast<AUDCLNT_STREAMOPTIONS>(0));
            try_candidate(AudioCategory_Speech, static_cast<AUDCLNT_STREAMOPTIONS>(0));
            try_candidate(AudioCategory_Other, static_cast<AUDCLNT_STREAMOPTIONS>(0));
        } else {
            // Render categories are mapped to OEM-specific processing graphs.
            // Consumer Realtek drivers can leave Media on their legacy graph
            // while neutral/RTC categories expose a genuinely shorter engine
            // period. Probe them all, then select by the reported duration.
            // Communications is Windows' RTC latency policy. It gets first
            // refusal only when the measured period ties; after initialization
            // we opt this session out of automatic ducking so karaoke/radio is
            // not attenuated. A genuinely shorter category still wins.
            try_candidate(AudioCategory_Communications, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_GameChat, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_Media, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_Other, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_Movie, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_SoundEffects, AUDCLNT_STREAMOPTIONS_RAW);
            try_candidate(AudioCategory_GameEffects, AUDCLNT_STREAMOPTIONS_RAW);
            // Keep the same non-ducking set as a normal-mode compatibility
            // fallback for endpoints that reject RAW entirely.
            try_candidate(AudioCategory_Communications, static_cast<AUDCLNT_STREAMOPTIONS>(0));
            try_candidate(AudioCategory_GameChat, static_cast<AUDCLNT_STREAMOPTIONS>(0));
            try_candidate(AudioCategory_Media, static_cast<AUDCLNT_STREAMOPTIONS>(0));
            try_candidate(AudioCategory_Other, static_cast<AUDCLNT_STREAMOPTIONS>(0));
            try_candidate(AudioCategory_Movie, static_cast<AUDCLNT_STREAMOPTIONS>(0));
            try_candidate(AudioCategory_SoundEffects, static_cast<AUDCLNT_STREAMOPTIONS>(0));
            try_candidate(AudioCategory_GameEffects, static_cast<AUDCLNT_STREAMOPTIONS>(0));
        }
        // Query every option first. Initializing even one candidate can lock
        // the endpoint's shared engine period, so probing/initializing in one
        // pass can prevent a later, shorter configuration from opening.
        std::stable_sort(candidates.begin(), candidates.end(), [](const auto& left, const auto& right) {
            return shared_audio::prefer_engine_candidate(
                left->period, left->format->nSamplesPerSec, left->raw,
                right->period, right->format->nSamplesPerSec, right->raw);
        });
        auto select = [&](std::unique_ptr<Candidate>& candidate) {
            client = candidate->client;
            format = candidate->format;
            candidate->format = nullptr;
            floating = candidate->floating;
            period = candidate->period;
            minimum_period = candidate->period;
            raw = candidate->raw;
            category = candidate->category;
        };
        bool periodicity_locked = false;
        for (auto& candidate : candidates) {
            // A queried period is not proof that a real driver accepts this
            // category/options pair. Try candidates shortest-first and retain
            // only one whose actual shared event stream initializes.
            if (initialize) {
                const HRESULT hr = candidate->client->InitializeSharedAudioStream(
                    AUDCLNT_STREAMFLAGS_EVENTCALLBACK, candidate->period, candidate->format, nullptr);
                if (hr == AUDCLNT_E_ENGINE_PERIODICITY_LOCKED) periodicity_locked = true;
                if (FAILED(hr)) continue;
            }
            select(candidate);
            break;
        }
        // A browser/radio/other shared client can already have fixed the
        // engine quantum. The documented recovery is to join that current
        // period. It may be larger than our requested minimum, but it keeps
        // monitoring functional and remains fully shared; when the lock goes
        // away, the normal monitor restart above requests the minimum again.
        if (!client && initialize && periodicity_locked) {
            for (const auto& blueprint : candidates) {
                auto current = std::make_unique<Candidate>();
                if (FAILED(device->Activate(__uuidof(IAudioClient3), CLSCTX_ALL, nullptr,
                                            reinterpret_cast<void**>(current->client.GetAddressOf())))) continue;
                AudioClientProperties properties{};
                properties.cbSize = sizeof(properties);
                properties.eCategory = blueprint->category;
                properties.Options = blueprint->options;
                if (FAILED(current->client->SetClientProperties(&properties))) continue;
                if (FAILED(current->client->GetCurrentSharedModeEnginePeriod(&current->format, &current->period)))
                    continue;
                if (!valid_format(current->format, current->floating)) continue;
                if (FAILED(current->client->InitializeSharedAudioStream(
                        AUDCLNT_STREAMFLAGS_EVENTCALLBACK, current->period, current->format, nullptr))) continue;
                current->raw = blueprint->raw;
                select(current);
                minimum_period = blueprint->period;
                period_locked = current->period > minimum_period;
                break;
            }
        }
        if (!client) throw std::runtime_error("No supported low-latency shared WASAPI configuration");
        if (!initialize) return;
        if (flow == eRender && category == AudioCategory_Communications) {
            ComPtr<IAudioSessionControl> session;
            ComPtr<IAudioSessionControl2> session2;
            if (SUCCEEDED(client->GetService(IID_PPV_ARGS(&session))) &&
                SUCCEEDED(session.As(&session2))) {
                // TRUE means this communications session handles attenuation
                // itself, so Windows must leave every other app/session alone.
                session2->SetDuckingPreference(TRUE);
            }
        }
        event.value = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!event.value) throw std::runtime_error("CreateEvent failed");
        check(client->SetEventHandle(event.value), "SetEventHandle");
        check(client->GetBufferSize(&buffer), "GetBufferSize");
    }
    double latency() {
        REFERENCE_TIME value = 0;
        return SUCCEEDED(client->GetStreamLatency(&value)) ? value / 10000.0 : -1;
    }
    void start() { check(client->Start(), "Start audio stream"); started = true; }
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
    Endpoint input, output, mirror;
    ComPtr<IAudioCaptureClient> capture;
    ComPtr<IAudioRenderClient> render, mirror_render;
    ComPtr<IAudioClock> clock;
    UINT64 clock_frequency = 0, written_frames = 0, drift_clock_position = 0;
    bool drift_clock_ready = false;
    std::unique_ptr<shared_audio::MonitorBuffer> queue, mirror_queue;
    std::unique_ptr<shared_audio::DeviceClockRateEstimator> device_clock_rate;
    std::vector<float> source, processed;
    Statistics stats;
    Process process = nullptr;
    HANDLE scheduling = nullptr;
    double pump_finished = 0;
    uint32_t requested_blocksize = 0;
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
    void open(const wchar_t* input_name, const wchar_t* output_name, const wchar_t* mirror_name,
              uint32_t blocksize, float requested_gain, bool input_exclusive,
              Info& info, bool initialize) {
        gain = requested_gain;
        requested_blocksize = blocksize;
        if (!blocksize || blocksize > 8192) throw std::runtime_error("Invalid fixed processing buffer");
        ComPtr<IMMDeviceEnumerator> enumerator;
        check(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, IID_PPV_ARGS(&enumerator)), "Create enumerator");
        input.open(enumerator.Get(), eCapture, input_name, blocksize, initialize, input_exclusive);
        output.open(enumerator.Get(), eRender, output_name, blocksize, initialize, false);
        if (mirror_name && *mirror_name)
            mirror.open(enumerator.Get(), eRender, mirror_name, blocksize, initialize, false);
        info = {input.format->nSamplesPerSec, output.format->nSamplesPerSec, blocksize, input.period, output.period,
                input.buffer, output.buffer, initialize ? input.latency() : -1, initialize ? output.latency() : -1,
                input.raw ? 1u : 0u, output.raw ? 1u : 0u,
                input.minimum_period, output.minimum_period,
                input.period_locked ? 1u : 0u, output.period_locked ? 1u : 0u,
                input.exclusive ? 1u : 0u, output.exclusive ? 1u : 0u};
        if (!initialize) return;
        check(input.client->GetService(IID_PPV_ARGS(&capture)), "Get capture service");
        check(output.client->GetService(IID_PPV_ARGS(&render)), "Get render service");
        if (mirror.client)
            check(mirror.client->GetService(IID_PPV_ARGS(&mirror_render)), "Get virtual microphone feed service");
        if (FAILED(output.client->GetService(IID_PPV_ARGS(&clock))) ||
            FAILED(clock->GetFrequency(&clock_frequency)) || !clock_frequency) clock.Reset();
        const double ratio = double(info.sample_rate) / info.output_sample_rate;
        const auto render_period_at_capture_rate = UINT32(std::ceil(output.period * ratio));
        const size_t capacity = shared_audio::monitor_queue_capacity(
            blocksize, input.period, render_period_at_capture_rate);
        // The driver period is allocation/callback cadence, not a latency
        // target. On consumer drivers it is commonly 10ms even when the user
        // selected 32/64 frames. Steering clock-drift correction toward a
        // whole period therefore manufactures that much queued delay inside
        // the app. Retain only the requested processing block (or less when
        // the endpoint period itself is shorter); spare capacity still
        // absorbs scheduling stalls without becoming intentional latency.
        // Do not retain a full capture period merely because capture is
        // exclusive. On consumer endpoints that period is often 10 ms and is
        // then pure extra user-mode latency. Spare allocation still handles
        // occasional scheduling stalls without making them the steady state.
        const size_t queue_target = shared_audio::monitor_queue_target(
            blocksize, input.period, render_period_at_capture_rate, input.exclusive);
        const size_t safety_frames = std::max<size_t>(2, std::min<size_t>(queue_target,
            size_t(std::ceil(output.period * ratio))));
        const uint64_t calibration_output_frames = shared_audio::drift_calibration_output_frames(
            input.period, render_period_at_capture_rate,
            input.format->nSamplesPerSec, output.format->nSamplesPerSec);
        queue = std::make_unique<shared_audio::MonitorBuffer>(
            capacity, ratio, safety_frames, calibration_output_frames,
            render_period_at_capture_rate);
        if (clock) {
            // Every WASAPI endpoint supplies its own device position correlated
            // to the same Windows QPC timebase. Use those oscillator slopes
            // for asynchronous resampling on every brand/device combination;
            // packet arrival order is only scheduler noise and must not alter
            // audible pitch.
            device_clock_rate = std::make_unique<shared_audio::DeviceClockRateEstimator>(
                info.sample_rate, info.output_sample_rate, clock_frequency);
            queue->prefer_device_clock();
        }
        if (!input.exclusive) {
            // A shared render wake may lead the matching capture wake by a
            // fraction of a period. Seed only the user's tiny processing
            // reserve as silence: this avoids startup starvation without
            // waiting for (and retaining) another whole hardware packet.
            queue->prime_silence();
        }
        if (mirror.client) {
            const double mirror_ratio = double(info.sample_rate) / mirror.format->nSamplesPerSec;
            const auto mirror_period_at_capture_rate = UINT32(std::ceil(mirror.period * mirror_ratio));
            const size_t mirror_capacity = shared_audio::monitor_queue_capacity(
                blocksize, input.period, mirror_period_at_capture_rate);
            const uint64_t mirror_calibration_output_frames =
                shared_audio::drift_calibration_output_frames(
                    input.period, mirror_period_at_capture_rate,
                    input.format->nSamplesPerSec, mirror.format->nSamplesPerSec);
            mirror_queue = std::make_unique<shared_audio::MonitorBuffer>(
                mirror_capacity, mirror_ratio, std::max<size_t>(2, std::min<size_t>(blocksize,
                    size_t(std::ceil(mirror.period * mirror_ratio)))),
                mirror_calibration_output_frames, mirror_period_at_capture_rate);
        }
        // Capture cannot expose any part of a packet before the endpoint's
        // physical period completes. Process that already-complete packet in
        // one callback instead of crossing C++ -> Python once per smaller UI
        // block (e.g. 30 calls for 16 requested frames on a 480-frame driver).
        const size_t processing_frames = shared_audio::processing_chunk_size(blocksize, input.period);
        source.resize(processing_frames);
        processed.resize(processing_frames);
    }
    void start(Process callback) {
        process = callback;
        DWORD task = 0;
        scheduling = AvSetMmThreadCharacteristicsW(L"Pro Audio", &task);
        // Registration selects the MMCSS task profile, but does not select
        // its highest realtime priority by itself. Keep only this pump thread
        // critical so renderer/AI load cannot make it miss an endpoint event.
        if (scheduling) AvSetMmThreadPriority(scheduling, AVRT_PRIORITY_CRITICAL);
        // Start the shared render clock empty before capture. WASAPI renders
        // silence while its buffer is empty; when the first complete capture
        // packet arrives it can therefore be submitted to an already-running
        // endpoint instead of waiting through an extra renderer startup
        // quantum. No silent frames are inserted into our timestamped queue.
        output.start();
        const uint32_t automatic_phase_delay =
            shared_audio::capture_start_phase_delay_us(
                input.exclusive, output.period, output.format->nSamplesPerSec);
        if (input.exclusive && output.event.value) {
            const DWORD boundary_timeout_ms = DWORD(std::min<uint64_t>(
                uint64_t(output.period) * 2000 / output.format->nSamplesPerSec + 50,
                250));
            WaitForSingleObject(output.event.value, boundary_timeout_ms);
        }
        wait_capture_start_phase(capture_start_delay_us(automatic_phase_delay));
        input.start();
    }
    void pump(uint32_t timeout) {
        const double entering = monotonic_seconds();
        stats.pump_gap_ms = pump_finished ? (entering - pump_finished) * 1000 : 0;
        HANDLE events[] = {input.event.value, output.event.value, mirror.event.value};
        const DWORD event_count = mirror.client ? 3 : 2;
        const DWORD awakened = WaitForMultipleObjects(event_count, events, FALSE, timeout);
        if (awakened == WAIT_FAILED)
            throw std::runtime_error("Audio event wait failed");
        const bool output_wakeup = awakened == WAIT_OBJECT_0 + 1;
        stats.event_wait_ms = (monotonic_seconds() - entering) * 1000;
        const UINT64 written_before_wait_service = written_frames;
        // Submit anything already available immediately, but do not sample
        // clock drift until the capture packet that woke at the same time has
        // also been drained below.
        render_ready(false);
        UINT32 available = 0;
        check(capture->GetNextPacketSize(&available), "GetNextPacketSize");
        // Drain capture after either event, handing each completed packet to
        // playback before processing the next queued packet.
        for (unsigned packet = 0; available && packet < 32; ++packet) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            UINT64 captured_position = 0, captured_qpc = 0;
            check(capture->GetBuffer(&data, &frames, &flags,
                                     &captured_position, &captured_qpc), "Capture GetBuffer");
            const double received_at = monotonic_seconds();
            if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) {
                ++stats.discontinuities;
                // The samples already queued (and the resampler's phase
                // against them) describe audio from before whatever gap the
                // driver just reported -- stitching new post-gap audio onto
                // them would keep the output timeline continuous but wrong.
                queue->reset(!input.exclusive);
                if (device_clock_rate) device_clock_rate->reset();
            }
            if (device_clock_rate && !(flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR))
                device_clock_rate->observe_capture(captured_position, captured_qpc);
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
                if (mirror_queue)
                    mirror_queue->push(processed.data(), count, 0, 0, received_at, monotonic_seconds());
                offset += count;
            }
            check(capture->ReleaseBuffer(frames), "Capture ReleaseBuffer");
            stats.capture_processing_ms = (monotonic_seconds() - received_at) * 1000;
            if (!ok) throw std::runtime_error("Microphone processing callback failed");
            stats.captured_frames += frames;
            // This call is driven by newly captured data, not by the output
            // clock. It may submit audio immediately, but must not teach the
            // asynchronous resampler that a normal capture burst is drift.
            render_ready(false);
            check(capture->GetNextPacketSize(&available), "GetNextPacketSize");
        }
        // The drift controller must observe the residual queue only after all
        // capture data already exposed by the endpoint has been consumed.
        // Otherwise a simultaneous render/capture wake is misclassified as
        // starvation and creates a repeating overflow/underrun cycle.
        const bool adjust_drift = shared_audio::should_adjust_drift(
            output_wakeup, available == 0);
        const bool submitted_since_wait = written_frames > written_before_wait_service;
        const bool output_starved = render_ready(adjust_drift, submitted_since_wait);
        // WaitForMultipleObjects returns the lowest-index signalled handle.
        // A faster capture event can therefore mask the render handle for a
        // long time even while the output clock advances. Sampling on every
        // completed pump observes each new clock position exactly once.
        observe_output_clock(output_starved);
        // The virtual microphone is an optional secondary consumer. Service
        // it only after the physical path and its clock accounting are done;
        // it must never add COM work ahead of what the singer hears.
        mirror_ready();
        pump_finished = monotonic_seconds();
    }
    void observe_output_clock(bool output_starved) {
        if (!output.started || !clock || !clock_frequency) return;
        UINT64 position = 0, qpc = 0;
        if (clock->GetPosition(&position, &qpc) != S_OK || !position || !qpc) return;
        if (device_clock_rate) {
            device_clock_rate->observe_output(position, qpc);
            if (device_clock_rate->ready())
                queue->set_device_clock_rate_ratio(device_clock_rate->rate_ratio());
        }
        if (!drift_clock_ready) {
            drift_clock_position = position;
            drift_clock_ready = true;
            // Do not interpret the packets used to prime playback as clock
            // drift; there was no output-clock interval to compare them to.
            return;
        }
        const UINT64 previous = drift_clock_position;
        drift_clock_position = position;
        const uint32_t maximum = std::max<uint32_t>(output.period,
            output.buffer) * 16;
        const uint32_t elapsed_frames = shared_audio::output_clock_elapsed_frames(
            position, previous, clock_frequency,
            output.format->nSamplesPerSec, maximum);
        if (!elapsed_frames) {
            return;
        }
        queue->nudge(elapsed_frames, output_starved ? elapsed_frames : 0);
    }
    void mirror_ready() {
        if (!mirror.client || !mirror_render || !mirror_queue) return;
        UINT32 padding = 0;
        check(mirror.client->GetCurrentPadding(&padding), "Get virtual microphone feed padding");
        const UINT32 target = std::min(mirror.period, mirror.buffer);
        const UINT32 ready = UINT32(mirror_queue->available());
        const UINT32 count = padding < target ? std::min(target - padding, ready) : 0;
        if (!count) return;
        BYTE* data = nullptr;
        check(mirror_render->GetBuffer(count, &data), "Virtual microphone feed GetBuffer");
        for (UINT32 index = 0; index < count; ++index) {
            float sample = 0;
            mirror_queue->pop(sample);
            mirror.write(data + index * mirror.format->nBlockAlign, sample);
        }
        check(mirror_render->ReleaseBuffer(count, 0), "Virtual microphone feed ReleaseBuffer");
        // Observe the queue left *after* this render transfer. Before-pop
        // fill includes the block we are about to consume and biases drift
        // correction toward a needless permanent backlog.
        mirror_queue->nudge(count);
        if (!mirror.started) mirror.start();
    }
    bool render_ready(bool adjust_drift, bool submitted_since_wait = false) {
        UINT32 padding = 0;
        check(output.client->GetCurrentPadding(&padding), "GetCurrentPadding");
        stats.render_padding_ms = double(padding) * 1000 / output.format->nSamplesPerSec;
        // Allocation capacity is NOT a target queue depth. Submit at most one
        // engine period instead of filling the entire Windows render buffer.
        const UINT32 target = shared_audio::render_padding_target(
            output.period, output.buffer, input.period,
            input.format->nSamplesPerSec, output.format->nSamplesPerSec,
            requested_blocksize);
        // An early render event must not enqueue a period of silence ahead of
        // microphone data that arrives a moment later. Submit only ready audio.
        const auto ready = UINT32(queue->available());
        const UINT32 count = shared_audio::render_transfer_count(
            padding, target, ready, output.started);
        // Windows wants audio and the queue has none at all -- the block
        // below (which is where underruns were counted) never runs in this
        // case, so a fully-starved queue was previously invisible in the
        // stats even though it is the more severe starvation than a partial
        // shortfall.
        // A low padding level is not itself an underrun: an input event can
        // wake this pump just before its packet is drained below. Count only
        // when the render engine has actually exhausted both sources.
        const bool fully_starved = shared_audio::render_starved(
            adjust_drift, padding, count, submitted_since_wait);
        if (fully_starved) ++stats.underruns;
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
        stats.resample_ratio = queue->rate_ratio();
        return fully_starved;
    }
};

#define API extern "C" __declspec(dllexport)
// Bump when exported structures change; prevent mixed DLL/Python layouts.
API uint32_t __cdecl wm_abi_version() { return 7; }
API void* __cdecl wm_open(const wchar_t* input, const wchar_t* output, const wchar_t* mirror,
                          uint32_t blocksize, float gain, uint32_t input_exclusive,
                          Info* info, char* error, uint32_t size) {
    try {
        auto engine = std::make_unique<Engine>();
        engine->open(input, output, mirror, blocksize, gain, input_exclusive != 0, *info, true);
        return engine.release();
    } catch (const std::exception& failure) { error_text(error, size, failure); return nullptr; }
}
API int __cdecl wm_probe(const wchar_t* input, const wchar_t* output, const wchar_t* mirror,
                         uint32_t blocksize, float gain, uint32_t input_exclusive,
                         Info* info, char* error, uint32_t size) {
    try { Engine engine; engine.open(input, output, mirror, blocksize, gain, input_exclusive != 0, *info, false); return 1; }
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
