import { describe, expect, it } from "vitest";

import { bboxToPercentage, pageAspectRatio } from "./geometry";

describe("page geometry", () => {
  it("converts PDF coordinates to percentages", () => {
    expect(
      bboxToPercentage({
        page: 1,
        page_size: { width: 200, height: 400 },
        bbox: { x0: 20, y0: 40, x1: 120, y1: 240 },
        rotation: 0,
      }),
    ).toEqual({ left: 10, top: 10, width: 50, height: 50 });
  });

  it("returns no overlay when source geometry is unavailable", () => {
    expect(
      bboxToPercentage({ page: 1, page_size: null, bbox: null, rotation: 0 }),
    ).toBeNull();
  });

  it("derives the fallback page ratio from canonical page_size", () => {
    expect(
      pageAspectRatio({
        page: 1,
        page_size: { width: 300, height: 600 },
        bbox: null,
        rotation: 0,
      }),
    ).toBe(0.5);
  });
});
