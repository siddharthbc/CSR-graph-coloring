"""
Bit-exact MT19937 + uniform_int_distribution matching libstdc++.

Needed so our Python output matches the C++ golden reference exactly
(same random draws, same color lists, same final coloring). Python's
built-in random module doesn't expose raw 32-bit draws the same way,
so we reimplement it here.

References:
    MT19937: Matsumoto & Nishimura, ACM TOMS 1998
    uniform_int: Lemire's nearly-divisionless method (arXiv:1805.10941)
"""

from __future__ import annotations


class MT19937:

    def __init__(self, seed: int = 123):
        self.mt = [0] * 624
        self.index = 624
        self.mt[0] = seed & 0xFFFFFFFF
        for i in range(1, 624):
            self.mt[i] = (
                1812433253 * (self.mt[i - 1] ^ (self.mt[i - 1] >> 30)) + i
            ) & 0xFFFFFFFF

    def _generate(self):
        for i in range(624):
            y = (self.mt[i] & 0x80000000) | (self.mt[(i + 1) % 624] & 0x7FFFFFFF)
            self.mt[i] = self.mt[(i + 397) % 624] ^ (y >> 1)
            if y & 1:
                self.mt[i] ^= 2567483615
        self.index = 0

    def __call__(self) -> int:
        if self.index >= 624:
            self._generate()
        y = self.mt[self.index]
        y ^= y >> 11
        y ^= (y << 7) & 2636928640
        y ^= (y << 15) & 4022730752
        y ^= y >> 18
        self.index += 1
        return y & 0xFFFFFFFF

    def uniform_int(self, a: int, b: int) -> int:
        """Random int in [a, b], Lemire's method (matches libstdc++)."""
        span = b - a
        if span == 0:
            # C++ std::uniform_int_distribution still consumes one engine
            # call even when the range is a single value.  We must do the
            # same to keep the RNG stream in sync.
            self()
            return a

        span_plus1 = span + 1
        raw = self()
        product = raw * span_plus1
        result = product >> 32
        leftover = product & 0xFFFFFFFF

        if leftover < span_plus1:
            threshold = (0x100000000 - span_plus1) % span_plus1
            while leftover < threshold:
                raw = self()
                product = raw * span_plus1
                result = product >> 32
                leftover = product & 0xFFFFFFFF

        return a + result
