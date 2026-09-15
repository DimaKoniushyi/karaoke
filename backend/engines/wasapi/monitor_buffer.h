#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <numeric>
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
inline uint32_t capture_start_phase_delay_us(bool input_exclusive,
                                             uint32_t render_period,
                                             uint32_t render_rate) {
    if (!input_exclusive || !render_period || !render_rate) return 0;
    const uint64_t period_us =
        (uint64_t(render_period) * 1000000 + render_rate / 2) / render_rate;
    // Fast renderers are already below the perceptible target, and delaying
    // their capture startup would add more scheduler uncertainty than it can
    // remove. Consumer shared endpoints with 5-10 ms quanta benefit from
    // starting exclusive capture near the middle of the render quantum: the
    // completed capture packet then reaches the next writable render slot
    // instead of missing it and waiting one full extra quantum. Keep a 10%
    // pre-boundary guard against scheduler/timer jitter: the exact half-way
    // point is the observed wrap boundary on 10-ms USB and WaveRT endpoints.
    if (period_us < 4000) return 0;
    const uint64_t aligned_phase_us =
        (uint64_t(render_period) * 400000 + render_rate / 2) / render_rate;
    return static_cast<uint32_t>(std::min<uint64_t>(aligned_phase_us, 20000));
}
inline uint32_t processing_chunk_size(uint32_t requested, uint32_t capture_period) {
    if (!requested || !capture_period)
        throw std::runtime_error("Invalid processing chunk size");
    return std::max(requested, capture_period);
}
inline uint32_t monitor_queue_target(uint32_t requested, uint32_t capture_period,
                                     uint32_t render_period, bool input_exclusive) {
    if (!requested || !capture_period || !render_period)
        throw std::runtime_error("Invalid monitor queue target");
    // Independent capture/render clocks expose whole, differently sized
    // packets in shared mode too. The maximum phase deficit is one capture
    // quantum minus the common alignment of both endpoint periods. Keep at
    // least the explicitly requested small scheduling reserve as well: equal
    // periods can signal in either order, and two interpolation frames alone
    // repeatedly starve a fast native dry-bypass path.
    const uint32_t phase_reserve = capture_period - std::gcd(capture_period, render_period);
    (void)input_exclusive;
    return std::max(requested, phase_reserve);
}
inline size_t monitor_queue_capacity(uint32_t requested, uint32_t capture_period,
                                     uint32_t render_period_at_capture_rate) {
    if (!requested || !capture_period || !render_period_at_capture_rate)
        throw std::runtime_error("Invalid monitor queue capacity");
    // Capacity is only an emergency allocation, never the latency target. One
    // additional capture packet absorbs the legal startup ordering where a
    // packet arrives just before the first render quantum becomes writable.
    return size_t(2) * std::max(capture_period, render_period_at_capture_rate)
        + size_t(requested) * 2 + capture_period;
}
inline uint64_t drift_calibration_output_frames(uint32_t capture_period,
                                                uint32_t render_period_at_capture_rate,
                                                uint32_t capture_rate,
                                                uint32_t output_rate) {
    if (!capture_period || !render_period_at_capture_rate || !capture_rate || !output_rate)
        throw std::runtime_error("Invalid drift calibration periods");
    // Packet delivery is quantised independently at both endpoints. Measure
    // clock drift only after a whole packet-phase cycle; otherwise a healthy
    // 48->160 or 133->441 cadence looks like oscillator drift and briefly
    // changes playback rate. Keep the former roughly two-second observation
    // duration, rounded up to an exact phase cycle in output-clock frames.
    const uint64_t phase_input_frames = std::lcm<uint64_t>(
        capture_period, render_period_at_capture_rate);
    const uint64_t phase_output_frames =
        (phase_input_frames * output_rate + capture_rate - 1) / capture_rate;
    if (!phase_output_frames)
        throw std::runtime_error("Invalid drift calibration cycle");
    const uint64_t minimum_output_frames = uint64_t(output_rate) * 2;
    return (minimum_output_frames + phase_output_frames - 1)
        / phase_output_frames * phase_output_frames;
}
inline uint32_t render_padding_target(uint32_t render_period, uint32_t render_buffer,
                                      uint32_t capture_period, uint32_t capture_rate,
                                      uint32_t render_rate, uint32_t requested) {
    if (!render_period || !render_buffer || !capture_period || !capture_rate ||
        !render_rate || !requested)
        throw std::runtime_error("Invalid render padding target");
    // Shared WASAPI consumes one complete engine quantum per render wake-up.
    // Some drivers do not expose decreasing padding to faster capture-driven
    // calls, so a partial target silently starves render and overflows input.
    (void)capture_period;
    (void)capture_rate;
    (void)render_rate;
    (void)requested;
    return std::min(render_period, render_buffer);
}
inline uint32_t render_transfer_count(uint32_t padding, uint32_t target,
                                      uint32_t ready, bool started) {
    if (!target || padding >= target) return 0;
    const uint32_t missing = target - padding;
    // Starting with a partial shared-engine quantum immediately underruns on
    // endpoints that expose padding only once per engine wake-up. Prime one
    // complete quantum; after start, accept every safe partial top-up.
    if (!started && ready < missing) return 0;
    return std::min(missing, ready);
}
inline bool render_starved(bool adjust_drift, uint32_t padding, uint32_t submitted_now,
                           bool submitted_since_wait) {
    return adjust_drift && !padding && !submitted_now && !submitted_since_wait;
}
inline bool should_adjust_drift(bool output_wakeup, bool capture_drain_complete) {
    // A shared render event may be observed at the same instant as an
    // exclusive capture packet. Measuring the residual queue before draining
    // that already-ready packet turns normal event ordering into a false
    // starvation signal and destabilizes the clock controller.
    return output_wakeup && capture_drain_complete;
}
inline uint32_t output_clock_elapsed_frames(uint64_t current_position,
                                            uint64_t previous_position,
                                            uint64_t clock_frequency,
                                            uint32_t sample_rate,
                                            uint32_t maximum_frames) {
    if (!clock_frequency || !sample_rate || !maximum_frames ||
        current_position <= previous_position)
        return 0;
    const double frames = double(current_position - previous_position)
        * double(sample_rate) / double(clock_frequency);
    if (!std::isfinite(frames) || frames < 0.5 || frames > maximum_frames)
        return 0;
    return static_cast<uint32_t>(std::llround(frames));
}
// Measures the two endpoint oscillators from device positions that Windows
// timestamps in one common QPC time domain.  Unlike counting delivered audio
// packets between render callbacks, this is independent of scheduler order,
// packet size and sample-rate conversion phase.
class DeviceClockRateEstimator {
    struct Observation {
        uint64_t position = 0;
        uint64_t qpc_100ns = 0;
        bool valid = false;
    };
    uint32_t input_rate, output_rate;
    uint64_t output_frequency, minimum_span_100ns;
    Observation capture_first, capture_latest, output_first, output_latest;
    double base_ratio, measured_ratio;
    bool measurement_ready = false;

    static void observe(Observation& first, Observation& latest,
                        uint64_t position, uint64_t qpc_100ns) {
        if (!qpc_100ns) return;
        if (!first.valid) {
            first = latest = {position, qpc_100ns, true};
            return;
        }
        // A delayed, duplicated or out-of-epoch reading is not evidence of
        // oscillator speed. Keep the last safe measurement until an explicit
        // stream discontinuity resets both endpoint epochs.
        if (qpc_100ns <= latest.qpc_100ns || position <= latest.position)
            return;
        latest = {position, qpc_100ns, true};
    }
    void update() {
        if (!capture_first.valid || !output_first.valid) return;
        const uint64_t capture_time = capture_latest.qpc_100ns - capture_first.qpc_100ns;
        const uint64_t output_time = output_latest.qpc_100ns - output_first.qpc_100ns;
        if (capture_time < minimum_span_100ns || output_time < minimum_span_100ns)
            return;
        const double capture_normalized =
            double(capture_latest.position - capture_first.position) * 10000000.0
            / (double(capture_time) * input_rate);
        const double output_normalized =
            double(output_latest.position - output_first.position) * 10000000.0
            / (double(output_time) * output_frequency);
        if (!std::isfinite(capture_normalized) || !std::isfinite(output_normalized)
            || capture_normalized < .98 || capture_normalized > 1.02
            || output_normalized < .98 || output_normalized > 1.02)
            return;
        const double candidate = base_ratio * capture_normalized / output_normalized;
        if (!std::isfinite(candidate) || candidate < base_ratio * .98
            || candidate > base_ratio * 1.02)
            return;
        measured_ratio = candidate;
        measurement_ready = true;
    }
public:
    DeviceClockRateEstimator(uint32_t requested_input_rate,
                             uint32_t requested_output_rate,
                             uint64_t requested_output_frequency,
                             uint64_t requested_minimum_span_100ns = 40000000) :
        input_rate(requested_input_rate), output_rate(requested_output_rate),
        output_frequency(requested_output_frequency),
        minimum_span_100ns(requested_minimum_span_100ns),
        base_ratio(double(requested_input_rate) / requested_output_rate),
        measured_ratio(base_ratio) {
        if (!input_rate || !output_rate || !output_frequency || !minimum_span_100ns)
            throw std::runtime_error("Invalid device clock estimator");
    }
    void observe_capture(uint64_t position, uint64_t qpc_100ns) {
        observe(capture_first, capture_latest, position, qpc_100ns);
        update();
    }
    void observe_output(uint64_t position, uint64_t qpc_100ns) {
        observe(output_first, output_latest, position, qpc_100ns);
        update();
    }
    bool ready() const { return measurement_ready; }
    double rate_ratio() const { return measured_ratio; }
    void reset() {
        capture_first = capture_latest = {};
        output_first = output_latest = {};
        measured_ratio = base_ratio;
        measurement_ready = false;
    }
};
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
    size_t pushed_since_nudge = 0;
    uint64_t clock_input_frames = 0, clock_output_frames = 0;
    uint64_t calibration_output_frames = 0;
    uint64_t next_calibration_output_frames = 0;
    size_t render_period_input_frames = 0;
    unsigned clock_observations = 0;
    bool clock_tracking_ready = false;
    bool device_clock_controlled = false;
    uint64_t lost = 0;
    double ratio, base_ratio, phase = 0, drift_correction = 0;
public:
    MonitorBuffer(size_t capacity, double rate_ratio, size_t requested_target_fill = 2,
                  uint64_t requested_calibration_output_frames = 0,
                  size_t requested_render_period_input_frames = 0) :
        samples(capacity), timestamps(capacity), received_times(capacity), processed_times(capacity),
        target_fill(std::clamp<size_t>(requested_target_fill, 2, capacity - 1)),
        calibration_output_frames(requested_calibration_output_frames),
        render_period_input_frames(requested_render_period_input_frames),
        ratio(rate_ratio), base_ratio(rate_ratio) {
        if (capacity < 2 || !std::isfinite(ratio) || ratio <= 0) throw std::runtime_error("Invalid monitor queue");
    }
    size_t size() const { return used; }
    // Exposed for deterministic native tests and runtime diagnostics. This is
    // the number of input frames consumed for one output frame.
    double rate_ratio() const { return ratio; }
    void prefer_device_clock() {
        // Production WASAPI supplies correlated device/QPC observations.
        // Keep nominal pitch until those clocks have enough span; never let
        // packet arrival phase or queue occupancy impersonate clock drift.
        device_clock_controlled = true;
        ratio = base_ratio;
        drift_correction = 0;
    }
    void set_device_clock_rate_ratio(double value) {
        if (!device_clock_controlled || !std::isfinite(value)
            || value < base_ratio * .98 || value > base_ratio * 1.02)
            return;
        ratio = value;
        drift_correction = value / base_ratio - 1.0;
    }
    void prime_silence() {
        // Seed only the deliberate low-latency reserve. These are not input
        // clock frames and therefore must not contribute to drift learning.
        // The method is safe to call once during startup (or again after a
        // no-op partial construction) without growing beyond target_fill.
        while (used < target_fill) {
            const size_t tail = (head + used) % samples.size();
            samples[tail] = 0.0f;
            timestamps[tail] = received_times[tail] = processed_times[tail] = 0.0;
            ++used;
        }
    }
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
    void reset(bool restore_reserve = false) {
        head = 0; used = 0; pushed_since_nudge = 0;
        clock_input_frames = clock_output_frames = 0;
        next_calibration_output_frames = 0;
        clock_observations = 0; clock_tracking_ready = false;
        phase = 0; ratio = base_ratio; drift_correction = 0;
        if (restore_reserve) prime_silence();
    }
    // Estimate independent endpoint rates from audio captured during actual
    // output-clock progress. Queue occupancy is deliberately excluded: a
    // scheduler stall or packet-phase burst is latency, not clock drift, and
    // must never become a permanent pitch/speed change.
    void nudge(size_t output_clock_frames = 0, size_t shortfall_frames = 0) {
        (void)shortfall_frames;
        if (!output_clock_frames) return;
        // A backlog larger than two render quanta is already stale real-time
        // monitoring audio (normally a suspended process/device). Recover in
        // one bounded discontinuity instead of audibly playing it faster for
        // many seconds. Ordinary packet phase never approaches this limit.
        const size_t output_clock_input_frames = std::max<size_t>(
            1, size_t(std::ceil(output_clock_frames * base_ratio)));
        // GetPosition is sampled on every capture-driven pump, so one delta
        // can be only a fraction of a complete render quantum. Never classify
        // legal packet phase as stale using less than the negotiated period.
        const size_t stale_quantum = std::max(
            output_clock_input_frames, render_period_input_frames);
        const size_t stale_limit = target_fill + stale_quantum * 2;
        if (used > stale_limit) {
            const size_t discard = used - target_fill;
            head = (head + discard) % samples.size();
            used = target_fill;
            lost += discard;
            phase = 0;
        }
        if (device_clock_controlled) {
            pushed_since_nudge = 0;
            return;
        }
        if (!clock_tracking_ready) {
            // Audio queued before playback obtains its first clock position is
            // startup priming, not evidence that the capture clock runs fast.
            pushed_since_nudge = 0;
            clock_tracking_ready = true;
            // Two complete phase-aligned windows distinguish a real clock
            // slope from one whole capture packet being assigned to the
            // neighbouring window at a scheduling boundary.
            next_calibration_output_frames = calibration_output_frames
                ? calibration_output_frames * 2 : 0;
            return;
        }
        clock_input_frames += pushed_since_nudge;
        clock_output_frames += output_clock_frames;
        pushed_since_nudge = 0;
        ++clock_observations;
        // Roughly two seconds at the common 10-ms shared render period makes
        // whole-packet phase error negligible while still learning the 0.5%
        // USB clock differences seen on real Razer/Realtek machines before
        // their small emergency queue can overflow.
        const bool calibration_ready = calibration_output_frames
            ? clock_output_frames >= next_calibration_output_frames
            : clock_observations >= 200;
        if (!calibration_ready || !clock_output_frames) return;
        const double measured_ratio = double(clock_input_frames)
            / double(clock_output_frames);
        const double relative_error = measured_ratio / base_ratio - 1.0;
        // A large mismatch is a missed/stalled device interval, not oscillator
        // drift. Keeping the previous safe rate avoids a robotic recovery.
        if (std::isfinite(relative_error) && std::abs(relative_error) <= 0.02) {
            // Packet delivery is quantised. Across this cumulative window a
            // single render-sized input packet is the maximum boundary error;
            // its relative size shrinks naturally as evidence accumulates.
            // Treat it as measurement uncertainty, not audible speed drift.
            const double packet_uncertainty = render_period_input_frames
                ? double(render_period_input_frames)
                    / (double(clock_output_frames) * base_ratio)
                : 0.0;
            drift_correction = std::abs(relative_error) <= packet_uncertainty
                ? 0.0
                : std::clamp(relative_error, -0.01, 0.01);
        }
        if (calibration_output_frames) {
            next_calibration_output_frames = clock_output_frames
                + calibration_output_frames;
        } else {
            // Preserve the deterministic legacy/test mode for callers that
            // have no negotiated phase-cycle threshold.
            clock_input_frames = clock_output_frames = 0;
            clock_observations = 0;
        }
        // Clock learning can initially leave a few milliseconds accumulated.
        // Drain only a clearly persistent (>2x target) backlog, cap the short
        // recovery slew at 0.5%, and remove it immediately at the target. It
        // cannot wind up or survive a recovered scheduler stall.
        const double recovery = used > target_fill * 2
            ? std::clamp(double(used - target_fill * 2)
                / std::max(2.0, double(samples.size())) * 0.02, 0.0, 0.005)
            : 0.0;
        ratio = base_ratio * (1.0 + std::clamp(
            drift_correction + recovery, -0.01, 0.01));
    }
    void push(const float* input, size_t count, double captured_at = 0, double step = 0,
              double received_at = 0, double processed_at = 0) {
        pushed_since_nudge += count;
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
