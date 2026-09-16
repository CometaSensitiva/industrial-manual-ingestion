"""Resolve printed page references to physical PDF page numbers."""

from __future__ import annotations

import re
from collections import defaultdict

from .models import (
    DocumentSignals,
    PageLine,
    PageNumberMap,
    PageNumberNamespace,
    PageRefObservation,
    ReconstructionConfig,
)


def infer_page_number_map(
    document: DocumentSignals,
    config: ReconstructionConfig,
) -> PageNumberMap:
    """Combine native PDF page labels with repeated printed header/footer evidence."""

    direct_refs: dict[str, int] = {}
    namespaces: dict[str, PageNumberNamespace] = {}

    for segment in document.page_labels:
        prefix = canonical_prefix(segment.prefix)
        if segment.style != "D":
            continue
        for pdf_page in range(segment.start_page, segment.end_page + 1):
            printed_page = segment.first_number + (pdf_page - segment.start_page)
            direct_refs[page_ref_key(prefix, printed_page)] = pdf_page
        namespaces[prefix] = PageNumberNamespace(
            prefix=prefix,
            pdf_start=segment.start_page,
            printed_start=segment.first_number,
            evidence_count=segment.end_page - segment.start_page + 1,
            source="pdf_page_labels",
        )

    observations = extract_page_ref_observations(document, config)
    for observation in observations:
        if observation.confidence >= 0.85:
            direct_refs.setdefault(
                page_ref_key(observation.prefix, observation.printed_page),
                observation.pdf_page,
            )

    inferred = infer_namespaces(observations, min_evidence=config.min_namespace_evidence)
    for prefix, namespace in inferred.items():
        namespaces.setdefault(prefix, namespace)

    return PageNumberMap(
        page_count=document.page_count,
        direct_refs=direct_refs,
        namespaces=namespaces,
    )


def extract_page_ref_observations(
    document: DocumentSignals,
    config: ReconstructionConfig,
) -> tuple[PageRefObservation, ...]:
    observations: list[PageRefObservation] = []
    for page in document.pages:
        for line in page.lines:
            if not is_header_or_footer(line, page.height, config.header_footer_fraction):
                continue
            observations.extend(_observations_from_line(line.text, page.number))
    return _dedupe_observations(observations)


def infer_namespaces(
    observations: tuple[PageRefObservation, ...],
    *,
    min_evidence: int,
) -> dict[str, PageNumberNamespace]:
    by_prefix_and_offset: dict[str, dict[int, list[PageRefObservation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for observation in observations:
        offset = observation.pdf_page - observation.printed_page
        by_prefix_and_offset[observation.prefix][offset].append(observation)

    namespaces: dict[str, PageNumberNamespace] = {}
    for prefix in sorted(by_prefix_and_offset):
        by_offset = by_prefix_and_offset[prefix]
        ranked = sorted(
            by_offset.items(),
            key=lambda item: (
                -len(item[1]),
                -sum(observation.confidence for observation in item[1]),
                abs(item[0]),
                item[0],
            ),
        )
        best_offset, evidence = ranked[0]
        if len(evidence) < min_evidence:
            continue
        first = min(evidence, key=lambda observation: observation.pdf_page)
        namespaces[prefix] = PageNumberNamespace(
            prefix=prefix,
            pdf_start=first.printed_page + best_offset,
            printed_start=first.printed_page,
            evidence_count=len(evidence),
            source="printed_page_numbers",
        )
    return namespaces


def parse_page_ref(text: str) -> tuple[str, int] | None:
    match = re.fullmatch(
        r"\s*(?:(?P<prefix>[A-Za-z]{1,12})-?)?(?P<number>\d{1,6})\s*",
        text,
    )
    if match is None:
        return None
    return canonical_prefix(match.group("prefix") or ""), int(match.group("number"))


def canonical_prefix(prefix: str) -> str:
    clean = prefix.strip().casefold().rstrip("-")
    return "" if clean in {"", "body"} else clean


def page_ref_key(prefix: str, printed_page: int) -> str:
    return f"{canonical_prefix(prefix)}:{printed_page}"


def is_header_or_footer(line: PageLine, page_height: float, fraction: float) -> bool:
    if line.bbox is None:
        return False
    return line.bbox[1] <= page_height * fraction or line.bbox[3] >= page_height * (1 - fraction)


def _observations_from_line(text: str, pdf_page: int) -> list[PageRefObservation]:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []

    observations: list[PageRefObservation] = []
    for match in re.finditer(
        r"(?<![A-Za-z0-9])(?P<prefix>[A-Za-z]{1,12})-(?P<number>\d{1,6})(?![A-Za-z0-9])",
        clean,
    ):
        observations.append(
            PageRefObservation(
                pdf_page=pdf_page,
                prefix=canonical_prefix(match.group("prefix")),
                printed_page=int(match.group("number")),
                confidence=0.95 if len(clean) <= 24 else 0.75,
                raw_text=clean,
            )
        )

    dashed = re.search(r"(?:^|\s)-\s*(?P<number>\d{1,6})\s*-(?:\s|$)", clean)
    if dashed is not None:
        observations.append(
            PageRefObservation(
                pdf_page=pdf_page,
                prefix="",
                printed_page=int(dashed.group("number")),
                confidence=0.9,
                raw_text=clean,
            )
        )

    if re.fullmatch(r"\d{1,6}", clean):
        observations.append(
            PageRefObservation(
                pdf_page=pdf_page,
                prefix="",
                printed_page=int(clean),
                confidence=0.7,
                raw_text=clean,
            )
        )
    return observations


def _dedupe_observations(
    observations: list[PageRefObservation],
) -> tuple[PageRefObservation, ...]:
    best: dict[tuple[int, str, int], PageRefObservation] = {}
    for observation in observations:
        key = (observation.pdf_page, observation.prefix, observation.printed_page)
        previous = best.get(key)
        if previous is None or observation.confidence > previous.confidence:
            best[key] = observation
    return tuple(
        sorted(
            best.values(),
            key=lambda observation: (
                observation.pdf_page,
                observation.prefix,
                observation.printed_page,
            ),
        )
    )
