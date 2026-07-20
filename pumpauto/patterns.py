from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    x_um: float
    y_um: float
    z_um: float


def point(x_um: float, y_um: float, z_um: float) -> list[Point]:
    return [Point(x_um, y_um, z_um)]


def line(start: Point, end: Point, count: int) -> list[Point]:
    if count < 1:
        raise ValueError("count must be >= 1")
    if count == 1:
        return [start]
    return [
        Point(
            start.x_um + (end.x_um - start.x_um) * i / (count - 1),
            start.y_um + (end.y_um - start.y_um) * i / (count - 1),
            start.z_um + (end.z_um - start.z_um) * i / (count - 1),
        )
        for i in range(count)
    ]


def raster(center: Point, width_um: float, height_um: float, nx: int, ny: int) -> list[Point]:
    if nx < 1 or ny < 1 or width_um < 0 or height_um < 0:
        raise ValueError("Invalid raster.")
    xs = [center.x_um] if nx == 1 else [center.x_um - width_um / 2 + width_um * i / (nx - 1) for i in range(nx)]
    ys = [center.y_um] if ny == 1 else [center.y_um - height_um / 2 + height_um * j / (ny - 1) for j in range(ny)]
    points: list[Point] = []
    for row, y in enumerate(ys):
        row_xs = xs if row % 2 == 0 else list(reversed(xs))
        points.extend(Point(x, y, center.z_um) for x in row_xs)
    return points


def validate(points: list[Point], ranges: dict[str, list[float]], max_points: int) -> None:
    if not points:
        raise ValueError("The pattern is empty.")
    if len(points) > max_points:
        raise ValueError(f"The pattern has {len(points)} points; limit: {max_points}.")
    for index, p in enumerate(points):
        for axis, value in (("x", p.x_um), ("y", p.y_um), ("z", p.z_um)):
            low, high = ranges[axis]
            if not low <= value <= high:
                raise ValueError(f"Point {index}: {axis}={value} µm is outside [{low}, {high}].")
