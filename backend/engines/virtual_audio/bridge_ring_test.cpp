#include "bridge_ring.h"
#include <array>
#include <cassert>
#include <cstdint>

int main() {
    advoice::BridgeRing<std::uint8_t, 8> ring;
    const std::array<std::uint8_t, 4> first{1, 2, 3, 4};
    ring.write(first.data(), first.size());

    std::array<std::uint8_t, 3> output{};
    assert(ring.read(output.data(), output.size()) == 3);
    assert((output == std::array<std::uint8_t, 3>{1, 2, 3}));

    const std::array<std::uint8_t, 8> newest{10, 11, 12, 13, 14, 15, 16, 17};
    ring.write(newest.data(), newest.size());
    std::array<std::uint8_t, 8> wrapped{};
    assert(ring.read(wrapped.data(), wrapped.size()) == 8);
    assert(wrapped == newest);

    std::array<std::uint8_t, 2> silence{9, 9};
    assert(ring.read(silence.data(), silence.size()) == 0);
    assert((silence == std::array<std::uint8_t, 2>{0, 0}));
}
