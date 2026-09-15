#include "monitor_buffer.h"
#include <iostream>
#include <limits>
using namespace shared_audio;
static int verification = 0;
static void verify(bool condition) {
    ++verification;
    if (!condition) {
        std::cerr << "Native audio test failed at check " << verification << '\n';
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
    // Windows delivers capture only once per physical engine period. Splitting
    // that already-complete 480-frame packet into thirty 16-frame Python DSP
    // calls cannot make it audible sooner; it only adds interpreter crossings.
    verify(processing_chunk_size(16, 480) == 480);
    verify(processing_chunk_size(64, 48) == 64);
    // Exclusive capture must not manufacture a whole additional capture
    // period in the user-mode drift queue.  A 441-frame Realtek period is
    // already 10 ms; retaining another one made the hybrid path measurably
    // slower even while capture and render clocks were healthy.
    verify(monitor_queue_target(16, 441, true) == 16);
    verify(monitor_queue_target(64, 480, false) == 64);
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
    // When capture and render events become ready together, the render event
    // must not teach the drift controller that the microphone is starved
    // before the already-ready capture packet has been drained. That false
    // negative correction accumulated for about 90 seconds, overflowed the
    // queue, then produced a burst of underruns and perceptible delay.
    verify(!should_adjust_drift(true, false));
    verify(should_adjust_drift(true, true));
    verify(!should_adjust_drift(false, true));
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
        low_latency_drift.nudge(64);
        for (int frame = 0; frame < 64; ++frame) low_latency_drift.pop(result);
    }
    verify(low_latency_drift.size() <= 96);
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
    // The inverse clock mismatch must converge as well. A slower capture
    // clock presents as a real render starvation signal; the controller may
    // adapt, but it must not manufacture a permanently slowed, robotic
    // stream merely because the healthy residual queue is empty.
    MonitorBuffer slow_usb_clock(992, 1.0, 16);
    uint64_t slow_clock_misses = 0;
    for (int tick = 0; tick < 12000; ++tick) {
        const size_t produced = tick % 5 < 3 ? 478 : 477; // average 477.6 / 480
        slow_usb_clock.push(skew_packet, produced);
        for (int frame = 0; frame < 480; ++frame) {
            if (!slow_usb_clock.pop(result)) {
                ++slow_clock_misses;
            }
        }
        slow_usb_clock.nudge(480);
    }
    verify(slow_clock_misses < 2000);
    verify(slow_usb_clock.dropped() < 100);
    // Equal-rate endpoints do not wake in equal-sized packets. The Razer
    // profile supplies 144-frame capture bursts while shared render consumes
    // 480-frame quanta. Over each three render periods the 432/432/576 input
    // cadence is exactly 480 frames on average. A controller must absorb this
    // normal phase pattern without winding its rate estimate to +/-1%, filling
    // the emergency queue and periodically throwing away live microphone data.
    MonitorBuffer burst_phased_clock(992, 1.0, 16);
    uint64_t burst_misses = 0;
    for (int tick = 0; tick < 30000; ++tick) {
        const size_t produced = tick % 3 == 2 ? 576 : 432;
        float burst[576]{};
        burst_phased_clock.push(burst, produced);
        for (int frame = 0; frame < 480; ++frame) {
            if (!burst_phased_clock.pop(result)) {
                ++burst_misses;
            }
        }
        burst_phased_clock.nudge(480);
    }
    verify(burst_phased_clock.dropped() < 100);
    verify(burst_phased_clock.size() < 160);
    verify(burst_misses < 2000);
    std::cout << "Native shared audio tests passed: periods, PCM/float, saturation, bounded queue, underrun, resampling\n";
}
