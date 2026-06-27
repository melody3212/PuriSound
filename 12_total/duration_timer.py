"""조건 누적 타이머 (legacy noise_controller 호환)."""


class DurationTimer:
    def __init__(self, threshold_sec: float = 5.0, tolerance_sec: float = 2.0):
        self.threshold_sec = threshold_sec
        self.tolerance_sec = tolerance_sec
        self.accumulated = 0.0
        self.break_time = 0.0

    def reset(self) -> None:
        self.accumulated = 0.0
        self.break_time = 0.0

    def update(self, condition: bool, dt: float) -> bool:
        if condition:
            self.accumulated += dt
            self.break_time = 0.0
        else:
            self.break_time += dt
            if self.break_time > self.tolerance_sec:
                self.accumulated = 0.0
        return self.accumulated >= self.threshold_sec