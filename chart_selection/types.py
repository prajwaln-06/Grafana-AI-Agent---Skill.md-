from dataclasses import dataclass

@dataclass
class ChartAnalysis:
    num_series: int
    num_points: int
    is_time_series: bool
    is_single_value: bool
    is_categorical: bool
    has_threshold: bool
