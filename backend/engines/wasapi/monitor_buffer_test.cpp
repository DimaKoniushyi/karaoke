#include "monitor_buffer.h"
#include <iostream>
#include <limits>
#include <source_location>
using namespace shared_audio;
static int verification = 0;
static void verify(bool condition, const std::source_location location =
                   std::source_location::current()) {
    ++verification;
    if (!condition) {
        std::cerr << "Native audio test failed at check " << verification
                  << " (line " << location.line() << ")\n";
        std::exit(1);
    }
}
int main() {
    // Windows Driver is a low-latency shared transport.  The UI processing
    // block is not an instruction to make the Windows engine period larger:
    // when the endpoint advertises a 48-frame minimum, a 64-frame processing
    // block must still run on that 48-frame engine period.
    verify(lowest_latency_engine_period(48, 480, 48) == 48);
    verify(lowest_latency_engine_period(47, 480, 48) == 48);
    verify(lowest_latency_engine_period(49, 480, 48) == 96);
    // Candidate periods are expressed in each candidate format's frames.
    // A speech-mode 160-frame period at 16kHz is the same 10ms as 480 frames
    // at 48kHz and must not displace the first (RAW, full-bandwidth) winner.
    verify(!shorter_engine_period(160, 16000, 480, 48000));
    verify(shorter_engine_period(240, 48000, 480, 48000));
    verify(!shorter_engine_period(480, 48000, 240, 48000));
    // The driver's measured engine period wins across RAW/non-RAW modes.
    // Preferring RAW unconditionally kept consumer Realtek endpoints at a
    // legacy 10 ms quantum even when their normal shared path advertised a
    // genuinely shorter period. RAW is only the tie-breaker.
    verify(!prefer_engine_candidate(480, 48000, true, 128, 48000, false));
    verify(prefer_engine_candidate(128, 48000, false, 480, 48000, true));
    verify(prefer_engine_candidate(480, 48000, true, 480, 48000, false));
    verify(prefer_engine_candidate(128, 48000, true, 480, 48000, true));
    // Capture packet arrival is quantised and scheduled independently from
    // render wake-ups.  Clock correction must therefore use each endpoint's
    // own device-position/QPC pair, not count whole packets between arbitrary
    // output observations.  Irregular observations of exact 16/48-kHz clocks
    // must remain exactly pitch-transparent.
    DeviceClockRateEstimator exact_clocks(16000, 48000, 48000, 40000000);
    exact_clocks.observe_capture(160, 1001000000);
    exact_clocks.observe_output(480, 1001000000);
    exact_clocks.observe_capture(16160, 1011000000);
    exact_clocks.observe_output(72480, 1015000000);
    exact_clocks.observe_capture(64160, 1041000000);
    exact_clocks.observe_output(192480, 1041000000);
    verify(exact_clocks.ready());
    verify(std::abs(exact_clocks.rate_ratio() - 16000.0 / 48000.0) < 1e-12);
    // A real +1000 ppm capture oscillator must be followed precisely, while
    // a +1000 ppm render oscillator requires the inverse correction.
    DeviceClockRateEstimator fast_capture(16000, 48000, 48000, 40000000);
    fast_capture.observe_capture(0, 1000000000);
    fast_capture.observe_output(0, 1000000000);
    fast_capture.observe_capture(64064, 1040000000);
    fast_capture.observe_output(192000, 1040000000);
    verify(std::abs(fast_capture.rate_ratio()
        - (16000.0 / 48000.0) * 1.001) < 1e-9);
    DeviceClockRateEstimator fast_output(16000, 48000, 48000, 40000000);
    fast_output.observe_capture(0, 1000000000);
    fast_output.observe_output(0, 1000000000);
    fast_output.observe_capture(64000, 1040000000);
    fast_output.observe_output(192192, 1040000000);
    verify(std::abs(fast_output.rate_ratio()
        - (16000.0 / 48000.0) / 1.001) < 1e-9);
    // Bad timestamps are ignored, and an explicit discontinuity forgets the
    // old clock epoch instead of stitching two unrelated slopes together.
    const double safe_ratio = fast_capture.rate_ratio();
    fast_capture.observe_capture(1, 1030000000);
    fast_capture.observe_output(1, 1030000000);
    verify(std::abs(fast_capture.rate_ratio() - safe_ratio) < 1e-12);
    fast_capture.reset();
    verify(!fast_capture.ready());
    verify(std::abs(fast_capture.rate_ratio() - 16000.0 / 48000.0) < 1e-12);
    MonitorBuffer hardware_clock_queue(1024, 16000.0 / 48000.0, 32, 480, 160);
    hardware_clock_queue.prefer_device_clock();
    float hardware_packet[320]{};
    for (int tick = 0; tick < 500; ++tick) {
        // Deliberately alternate packet assignment around each render wake.
        // Once device-clock mode is selected, this scheduler pattern is not
        // allowed to overwrite the hardware-derived ratio.
        const size_t frames = tick % 2 ? 320 : 0;
        hardware_clock_queue.push(hardware_packet, frames);
        hardware_clock_queue.nudge(480);
    }
    verify(std::abs(hardware_clock_queue.rate_ratio()
        - 16000.0 / 48000.0) < 1e-12);
    hardware_clock_queue.set_device_clock_rate_ratio(
        (16000.0 / 48000.0) * 1.00025);
    hardware_clock_queue.nudge(480);
    verify(std::abs(hardware_clock_queue.rate_ratio()
        - (16000.0 / 48000.0) * 1.00025) < 1e-12);
    // Windows delivers capture only once per physical engine period. Splitting
    // that already-complete 480-frame packet into thirty 16-frame Python DSP
    // calls cannot make it audible sooner; it only adds interpreter crossings.
    verify(processing_chunk_size(16, 480) == 480);
    verify(processing_chunk_size(64, 48) == 64);
    // Exclusive capture must not manufacture a whole additional capture
    // period in the user-mode drift queue.  A 441-frame Realtek period is
    // already 10 ms; retaining another one made the hybrid path measurably
    // slower even while capture and render clocks were healthy.
    verify(monitor_queue_target(16, 133, 441, true) == 126);
    verify(monitor_queue_target(16, 48, 160, true) == 32);
    // The Jabra 16->48-kHz startup can expose one capture packet just before
    // the first render quantum is consumed. Emergency allocation must absorb
    // that phase without changing the steady-state target or dropping audio.
    verify(monitor_queue_capacity(16, 48, 160) == 400);
    verify(drift_calibration_output_frames(48, 160, 16000, 48000) == 96480);
    // gcd(133, 441) is seven: their phase repeats every 19 render periods.
    // The first whole-cycle boundary after two seconds is 209 periods.
    verify(drift_calibration_output_frames(133, 441, 44100, 44100) == 92169);
    // The requested low-latency block is the minimum scheduling reserve.
    // Two nominally equal shared periods can signal in either order, so two
    // interpolation frames alone repeatedly starve a fast dry-bypass path.
    // Differently sized periods additionally require C-gcd(C,R), while a
    // deliberately larger user buffer remains an explicit stability choice.
    verify(monitor_queue_target(64, 480, 480, false) == 64);
    verify(monitor_queue_target(64, 144, 480, false) == 96);
    verify(monitor_queue_target(256, 144, 480, false) == 256);
    // A shared renderer whose engine wakes once per 480-frame period must be
    // given the whole period. On the real Audient profile, targeting the
    // 144-frame capture period rendered only ~14k of the ~48k captured frames
    // per second, overflowed the queue and produced robotic gaps.
    verify(render_padding_target(480, 960, 144, 48000, 48000, 16) == 480);
    verify(render_padding_target(480, 960, 480, 48000, 48000, 16) == 480);
    verify(render_padding_target(441, 882, 144, 48000, 44100, 16) == 441);
    // Do not start a 441-frame shared renderer with only the first 133-frame
    // capture packet. It would run dry before the next shared-engine wake and
    // can remain in a permanently starved, robotic cadence. Once started,
    // partial top-ups from faster capture events are still useful.
    verify(render_transfer_count(0, 441, 133, false) == 0);
    verify(render_transfer_count(0, 441, 532, false) == 441);
    verify(render_transfer_count(0, 441, 133, true) == 133);
    verify(render_transfer_count(200, 441, 300, true) == 241);
    // Some shared drivers do not reflect ReleaseBuffer in GetCurrentPadding
    // until the next engine wake. A second check in the same pump may still
    // read zero, but audio submitted earlier in that pump means the renderer
    // was serviced and must not be reported as starved.
    verify(!render_starved(true, 0, 0, true));
    verify(render_starved(true, 0, 0, false));
    verify(!render_starved(false, 0, 0, false));
    verify(!render_starved(true, 1, 0, false));
    verify(!render_starved(true, 0, 1, false));
    // When capture and render events become ready together, the render event
    // must not teach the drift controller that the microphone is starved
    // before the already-ready capture packet has been drained. That false
    // negative correction accumulated for about 90 seconds, overflowed the
    // queue, then produced a burst of underruns and perceptible delay.
    verify(!should_adjust_drift(true, false));
    verify(should_adjust_drift(true, true));
    verify(!should_adjust_drift(false, true));
    // A coalesced WASAPI event can represent more than one nominal engine
    // period. Drift estimation must use the hardware clock delta instead of
    // pretending that every event always advances by exactly one period.
    verify(output_clock_elapsed_frames(480, 0, 48000, 48000, 1920) == 480);
    verify(output_clock_elapsed_frames(1440, 480, 48000, 48000, 1920) == 960);
    verify(output_clock_elapsed_frames(480, 480, 48000, 48000, 1920) == 0);
    verify(output_clock_elapsed_frames(400, 480, 48000, 48000, 1920) == 0);
    verify(output_clock_elapsed_frames(4800, 480, 48000, 48000, 1920) == 0);
    verify(engine_period(64, 48, 480, 48) == 96);
    verify(engine_period(64, 441, 441, 441) == 441);
    verify(engine_period(128, 32, 1024, 32) == 128);
    // A request above the device's maximum is clamped into range (matching
    // the ASIO bridge's resolve_buffer_size), not rejected.
    verify(engine_period(2048, 48, 480, 48) == 480);
    bool rejected = false;
    try { engine_period(64, 480, 48, 48); } catch (const std::exception&) { rejected = true; }
    verify(rejected);
    for (unsigned bits : {16, 24, 32}) for (float value : {-.9F, 0.0F, .9F}) {
        uint8_t data[4]{};
        encode(data, bits, false, value);
        verify(std::abs(decode(data, bits, false) - value) < .0001);
    }
    uint8_t data[4]{};
    encode(data, 32, true, std::numeric_limits<float>::quiet_NaN());
    verify(decode(data, 32, true) == 0);
    const float source[] = {1, 2, 3, 4, 5, 6};
    MonitorBuffer direct(4, 1);
    direct.push(source, 6);
    verify(direct.size() == 4 && direct.dropped() == 2);
    float result = 0;
    for (float expected : {3, 4, 5, 6}) { verify(direct.pop(result)); verify(result == expected); }
    verify(!direct.pop(result) && result == 0);
    // Arrival/processing clocks must follow the exact sample through overwrite
    // and resampling, independently of missing device capture timestamps.
    MonitorBuffer timing(4, 1);
    timing.push(source, 2, 0, 0, 12, 12.001);
    timing.push(source, 4, 0, 0, 13, 13.002);
    double captured = -1, received = -1, processed = -1;
    verify(timing.pop(result, &captured, &received, &processed));
    verify(captured == 0 && received == 13 && processed == 13.002);
    MonitorBuffer interpolatedTiming(8, .5);
    interpolatedTiming.push(source, 1, 10, 0, 12, 12.001);
    interpolatedTiming.push(source + 1, 1, 11, 0, 13, 13.001);
    verify(interpolatedTiming.pop(result, &captured, &received, &processed));
    verify(captured == 10 && received == 12);
    verify(interpolatedTiming.pop(result, &captured, &received, &processed));
    verify(captured == 10.5 && received == 12.5 && std::abs(processed - 12.501) < 1e-9);
    verify(!interpolatedTiming.pop(result, &captured, &received, &processed));
    verify(captured == 0 && received == 0 && processed == 0);
    MonitorBuffer upsample(16, .5);
    upsample.push(source, 6);
    for (float expected : {1.0F, 1.5F, 2.0F, 2.5F}) { verify(upsample.pop(result)); verify(result == expected); }
    MonitorBuffer downsample(16, 2);
    downsample.push(source, 6);
    for (float expected : {1, 3, 5}) { verify(downsample.pop(result)); verify(result == expected); }
    verify(!downsample.pop(result));
    for (double ratio : {44100.0 / 48000.0, 48000.0 / 44100.0, .5, 2.0}) {
        MonitorBuffer converted(2048, ratio);
        float packet[441]{};
        for (unsigned i = 0; i < 2000; ++i) {
            converted.push(packet, 441, 10.0 + i * .01, 1.0 / 44100);
            const size_t ready = converted.available();
            for (size_t frame = 0; frame < ready; ++frame) {
                double timestamp = 0;
                verify(converted.pop(result, &timestamp));
                verify(timestamp >= 10.0);
            }
            verify(converted.size() <= 2);
        }
        verify(converted.dropped() == 0);
    }
    // Long mismatched-clock simulation must remain bounded, never accumulating
    // seconds of old microphone audio when render misses a wake-up.
    MonitorBuffer bounded(256, 44100.0 / 48000.0);
    float block[64]{};
    for (int tick = 0; tick < 10000; ++tick) {
        bounded.push(block, 64);
        for (int frame = 0; frame < 60; ++frame) bounded.pop(result);
        verify(bounded.size() <= 256);
    }
    // Drift correction is allowed to keep one small safety period, but must
    // never steer a healthy same-rate stream toward half of its emergency
    // allocation. Half-full here would add ~10ms at 48kHz solely inside the
    // application, even though capture and render are keeping pace.
    MonitorBuffer low_latency_drift(1024, 1.0, 64);
    for (int tick = 0; tick < 100000; ++tick) {
        low_latency_drift.push(block, 64);
        for (int frame = 0; frame < 64; ++frame) low_latency_drift.pop(result);
        low_latency_drift.nudge(64);
    }
    verify(low_latency_drift.size() <= 96);
    // Shared capture/render endpoints can expose equal 10-ms packets with the
    // render wake arriving a fraction before capture. Start with exactly the
    // requested tiny user-mode reserve as silence, then the first real packet
    // can fill a complete render quantum while the reserve remains intact.
    // Waiting for a second whole packet would add 10 ms; starting empty causes
    // audible starvation while drift recovery slowly manufactures the reserve.
    MonitorBuffer primed_shared(2048, 1.0, 64, 88200, 441);
    primed_shared.prime_silence();
    verify(primed_shared.size() == 64);
    float first_shared_packet[441]{};
    primed_shared.push(first_shared_packet, 441);
    for (int frame = 0; frame < 441; ++frame)
        verify(primed_shared.pop(result));
    verify(primed_shared.size() == 64);
    verify(primed_shared.rate_ratio() == 1.0);
    primed_shared.reset(true);
    verify(primed_shared.size() == 64);
    primed_shared.reset(false);
    verify(primed_shared.size() == 0);
    // A perfectly synchronous input/output pair must remain bit-transparent.
    // The controller is sampled after each render transfer, so an empty
    // residual queue is the healthy steady state, not evidence that capture
    // is running slow.  Treating it as negative drift eventually changed the
    // resampling ratio by -2%, producing the reported robotic/warbling voice.
    MonitorBuffer transparent_clock(1024, 1.0, 64);
    uint64_t transparent_frame = 0;
    for (int tick = 0; tick < 6000; ++tick) {
        float signal[64]{};
        for (float& sample : signal) sample = float(++transparent_frame);
        transparent_clock.push(signal, 64);
        for (float expected : signal) {
            verify(transparent_clock.pop(result));
            verify(result == expected);
        }
        transparent_clock.nudge(64);
    }
    verify(transparent_clock.size() == 0);
    verify(transparent_clock.dropped() == 0);
    // The Razer USB profile captured in production delivered about 0.5%
    // more capture frames than its shared render clock consumed.  A fixed
    // +/-0.02% proportional correction overflowed the queue and discarded
    // tens of thousands of frames, leaving ~8ms of stale voice buffered.
    MonitorBuffer usb_clock_skew(992, 1.0, 16);
    float skew_packet[483]{};
    for (int tick = 0; tick < 12000; ++tick) {
        const size_t produced = tick % 5 < 2 ? 483 : 482; // average 482.4 / 480
        usb_clock_skew.push(skew_packet, produced);
        for (int frame = 0; frame < 480; ++frame) usb_clock_skew.pop(result);
        usb_clock_skew.nudge(480);
    }
    verify(usb_clock_skew.dropped() < 100);
    verify(usb_clock_skew.size() < 160);
    verify(std::abs(usb_clock_skew.rate_ratio() - 1.005) < 0.001);
    // The inverse clock mismatch must converge as well. A slower capture
    // clock presents as a real render starvation signal; the controller may
    // adapt, but it must not manufacture a permanently slowed, robotic
    // stream merely because the healthy residual queue is empty.
    MonitorBuffer slow_usb_clock(992, 1.0, 16);
    uint64_t slow_clock_misses = 0;
    for (int tick = 0; tick < 12000; ++tick) {
        const size_t produced = tick % 5 < 3 ? 478 : 477; // average 477.6 / 480
        slow_usb_clock.push(skew_packet, produced);
        size_t shortfall = 0;
        for (int frame = 0; frame < 480; ++frame) {
            if (!slow_usb_clock.pop(result)) {
                ++slow_clock_misses;
                ++shortfall;
            }
        }
        slow_usb_clock.nudge(480, shortfall);
    }
    verify(slow_clock_misses < 2000);
    verify(slow_usb_clock.dropped() < 100);
    // Equal-rate endpoints do not wake in equal-sized packets. The Razer
    // profile supplies 144-frame capture bursts while shared render consumes
    // 480-frame quanta. Over each three render periods the 432/432/576 input
    // cadence is exactly 480 frames on average. A controller must absorb this
    // normal phase pattern without winding its rate estimate to +/-1%, filling
    // the emergency queue and periodically throwing away live microphone data.
    MonitorBuffer burst_phased_clock(992, 1.0, 96);
    uint64_t burst_misses = 0;
    for (int tick = 0; tick < 30000; ++tick) {
        const size_t produced = tick % 3 == 2 ? 576 : 432;
        float burst[576]{};
        burst_phased_clock.push(burst, produced);
        size_t shortfall = 0;
        for (int frame = 0; frame < 480; ++frame) {
            if (!burst_phased_clock.pop(result)) {
                ++burst_misses;
                ++shortfall;
            }
        }
        burst_phased_clock.nudge(480, shortfall);
    }
    verify(burst_phased_clock.dropped() < 100);
    verify(burst_phased_clock.size() <= 192);
    verify(burst_misses < 2000);
    // A 44.1-kHz exclusive endpoint commonly exposes 133-frame packets while
    // shared render advances by 441 frames. Sixty output events do not contain
    // an integer number of capture packets, so resetting the rate estimate at
    // each window alternates false fast/slow corrections and eventually makes
    // a healthy stream underrun or retain several milliseconds of stale audio.
    MonitorBuffer non_integral_packet_clock(992, 1.0, 126);
    float initial_packet_phase[532]{};
    non_integral_packet_clock.push(initial_packet_phase, 532);
    for (int frame = 0; frame < 441; ++frame) verify(non_integral_packet_clock.pop(result));
    uint64_t packet_accumulator = 0;
    uint64_t packet_clock_misses = 0;
    size_t packet_clock_peak = 0;
    size_t packet_clock_tail_min = 992, packet_clock_tail_max = 0;
    for (int tick = 0; tick < 30000; ++tick) {
        packet_accumulator += 441;
        const size_t packets = size_t(packet_accumulator / 133);
        packet_accumulator %= 133;
        float packets_buffer[532]{};
        non_integral_packet_clock.push(packets_buffer, packets * 133);
        for (int frame = 0; frame < 441; ++frame)
            if (!non_integral_packet_clock.pop(result)) ++packet_clock_misses;
        non_integral_packet_clock.nudge(441, 0);
        packet_clock_peak = std::max(packet_clock_peak, non_integral_packet_clock.size());
        if (tick >= 1000) {
            packet_clock_tail_min = std::min(packet_clock_tail_min, non_integral_packet_clock.size());
            packet_clock_tail_max = std::max(packet_clock_tail_max, non_integral_packet_clock.size());
        }
    }
    verify(non_integral_packet_clock.dropped() < 100);
    verify(packet_clock_peak <= 266);
    verify(packet_clock_tail_max - packet_clock_tail_min <= 266);
    verify(packet_clock_misses < 200);
    // Sampling packetised capture at an output-clock boundary can put one
    // complete Jabra packet in the adjacent two-second window: 199 packets
    // followed by 201, while the true long-term 16k/48k ratio is exact. That
    // quantisation must not become a temporary +/-0.5% playback-speed change.
    MonitorBuffer boundary_jitter_clock(1024, 16000.0 / 48000.0, 32,
                                        480 * 200, 160);
    boundary_jitter_clock.prime_silence();
    boundary_jitter_clock.nudge(480);  // output-clock baseline
    float jitter_packet[320]{};
    for (int tick = 0; tick < 400; ++tick) {
        const size_t produced = tick == 199 ? 0 : tick == 200 ? 320 : 160;
        boundary_jitter_clock.push(jitter_packet, produced);
        // Drain exactly what arrived so this isolates clock-window
        // quantisation from the separate queue-reserve controller.
        for (size_t frame = 0; frame < produced * 3; ++frame)
            boundary_jitter_clock.pop(result);
        boundary_jitter_clock.nudge(480);
        if (tick == 199)
            verify(std::abs(boundary_jitter_clock.rate_ratio()
                - 16000.0 / 48000.0) < 0.00001);
    }
    verify(std::abs(boundary_jitter_clock.rate_ratio()
        - 16000.0 / 48000.0) < 0.00001);
    // A scheduler or USB-driver stall may temporarily leave a large backlog.
    // Once equal-rate capture/render resumes, that transient must not remain
    // stored as a permanent resampling correction: doing so changes pitch and
    // produces the robotic voice reported on otherwise healthy endpoints.
    MonitorBuffer recovered_after_stall(4096, 1.0, 96);
    float recovery_backlog[480 * 4]{};
    float recovery_packet[480]{};
    recovered_after_stall.push(recovery_backlog, 480 * 4);
    // Two seconds is already perceptibly long; recovery must be complete by
    // then instead of letting the displayed/audible delay crawl for minutes.
    for (int tick = 0; tick < 200; ++tick) {
        recovered_after_stall.push(recovery_packet, 480);
        for (int frame = 0; frame < 480; ++frame)
            recovered_after_stall.pop(result);
        recovered_after_stall.nudge(480);
        verify(std::abs(recovered_after_stall.rate_ratio() - 1.0) < 1e-12);
    }
    verify(std::abs(recovered_after_stall.rate_ratio() - 1.0) < 0.0001);
    // Output-clock frames and queue frames use different units when a USB
    // headset captures at 16 kHz and renders at 48 kHz. A 600-frame input
    // backlog is far beyond two 10-ms render quanta (2 * 160 input frames)
    // and must be discarded instead of being mistaken for only 12.5 ms.
    MonitorBuffer mismatched_rate_stall(1024, 16000.0 / 48000.0, 32);
    float mismatched_backlog[600]{};
    mismatched_rate_stall.push(mismatched_backlog, 600);
    mismatched_rate_stall.nudge(480);
    verify(mismatched_rate_stall.size() == 32);
    // The output clock is sampled on every faster capture wake. Its latest
    // delta may therefore be only 144 output frames (48 input frames), even
    // though the shared renderer consumes 160 input-equivalent frames per
    // complete period. A legal packet-phase backlog of 146 frames is not a
    // stalled queue and must never be discarded based on that partial delta.
    MonitorBuffer jabra_packet_phase(400, 16000.0 / 48000.0, 32, 96480, 160);
    float jabra_phase[146]{};
    jabra_packet_phase.push(jabra_phase, 146);
    jabra_packet_phase.nudge(144);
    verify(jabra_packet_phase.size() == 146);
    verify(jabra_packet_phase.dropped() == 0);
    // Queue occupancy and packet phase are scheduler state, not oscillator
    // measurements. They must never modulate pitch: doing so made Jabra and
    // Logitech voices audibly robotic even with zero drops or underruns.
    MonitorBuffer low_jabra_reserve(400, 16000.0 / 48000.0, 32, 480, 160);
    float one_frame[1]{};
    float one_render_quantum[160]{};
    low_jabra_reserve.push(one_frame, 1);
    low_jabra_reserve.nudge(480);  // establish output-clock baseline
    low_jabra_reserve.push(one_render_quantum, 160);
    for (int frame = 0; frame < 480; ++frame) low_jabra_reserve.pop(result);
    low_jabra_reserve.nudge(480);
    low_jabra_reserve.push(one_render_quantum, 160);
    for (int frame = 0; frame < 480; ++frame) low_jabra_reserve.pop(result);
    low_jabra_reserve.nudge(480);
    verify(std::abs(low_jabra_reserve.rate_ratio()
        - 16000.0 / 48000.0) < 1e-12);
    // Shared capture and render quanta must overlap in time.  Starting an
    // exclusive capture immediately after a 10-ms shared renderer made the
    // two device periods nearly sequential (15.94 ms measured on Jabra).
    // Guarded mid-period phase alignment consistently reduced it to 10.94 ms and
    // also reduced an unrelated Audient Windows path from 18.71 to 9.82 ms.
    verify(shared_audio::capture_start_phase_delay_us(true, 480, 48000) == 4000);
    verify(shared_audio::capture_start_phase_delay_us(true, 441, 44100) == 4000);
    // Do not phase-shift fully shared capture or an already sub-4-ms renderer.
    verify(shared_audio::capture_start_phase_delay_us(false, 480, 48000) == 0);
    verify(shared_audio::capture_start_phase_delay_us(true, 144, 48000) == 0);
    std::cout << "Native shared audio tests passed: periods, PCM/float, saturation, bounded queue, underrun, resampling\n";
}
