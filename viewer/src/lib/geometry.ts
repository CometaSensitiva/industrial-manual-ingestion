import type { SourceReference } from "./types";

export interface PercentageGeometry {
  left: number;
  top: number;
  width: number;
  height: number;
}

function clamp(value: number): number {
  return Math.min(100, Math.max(0, value));
}

export function bboxToPercentage(
  source: SourceReference,
): PercentageGeometry | null {
  if (!source.bbox || !source.page_size) return null;

  const { bbox, page_size: pageSize } = source;
  if (pageSize.width <= 0 || pageSize.height <= 0) return null;

  const left = clamp((bbox.x0 / pageSize.width) * 100);
  const top = clamp((bbox.y0 / pageSize.height) * 100);
  const right = clamp((bbox.x1 / pageSize.width) * 100);
  const bottom = clamp((bbox.y1 / pageSize.height) * 100);

  if (right <= left || bottom <= top) return null;
  return {
    left,
    top,
    width: right - left,
    height: bottom - top,
  };
}

export function pageAspectRatio(source: SourceReference | null): number {
  if (!source?.page_size) return 1 / Math.SQRT2;
  return source.page_size.width / source.page_size.height;
}
