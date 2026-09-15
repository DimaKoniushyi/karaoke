#pragma once

#include <algorithm>
#include <cstddef>

namespace advoice {

// Storage-only single-lock ring. The kernel adapter owns synchronization so
// neither producer nor consumer ever allocates or waits while audio is live.
template <typename Sample, std::size_t Capacity>
class BridgeRing {
    static_assert(Capacity > 0);

public:
    void write(const Sample* source, std::size_t count) noexcept {
        if (count >= Capacity) {
            source += count - Capacity;
            count = Capacity;
            read_ = write_ = size_ = 0;
        }
        while (count--) {
            data_[write_] = *source++;
            write_ = (write_ + 1) % Capacity;
            if (size_ == Capacity) read_ = (read_ + 1) % Capacity;
            else ++size_;
        }
    }

    std::size_t read(Sample* target, std::size_t count) noexcept {
        const std::size_t available = std::min(count, size_);
        for (std::size_t index = 0; index < available; ++index) {
            target[index] = data_[read_];
            read_ = (read_ + 1) % Capacity;
        }
        size_ -= available;
        std::fill(target + available, target + count, Sample{});
        return available;
    }

    std::size_t size() const noexcept { return size_; }

private:
    Sample data_[Capacity]{};
    std::size_t read_ = 0;
    std::size_t write_ = 0;
    std::size_t size_ = 0;
};

} // namespace advoice
