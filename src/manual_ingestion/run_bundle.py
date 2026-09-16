"""Atomic workspace and writer for the canonical V2 run bundle."""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath
from types import TracebackType
from uuid import uuid4

from .models import (
    ArtifactPaths,
    ManualDocument,
    RunManifest,
    RunStatus,
    TocEntry,
    ValidationReport,
)


class RunBundleExistsError(FileExistsError):
    pass


class RunBundleWorkspace:
    """One adjacent staging directory used from parsing through publication.

    Profiles should write their assets and diagnostics directly below ``root``.
    Canonical JSON files can then be staged, inspected or validated, and finally
    published with one rename on the same filesystem.
    """

    def __init__(self, output_dir: str | Path, *, artifacts: ArtifactPaths | None = None) -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.artifacts = artifacts or ArtifactPaths()
        self._staging_dir: Path | None = None
        self._published = False
        self._staged_artifacts: ArtifactPaths | None = None
        self._owned_paths: set[str] = set()

    @property
    def root(self) -> Path:
        """Current writable staging root, or the final root after publication."""

        if self._staging_dir is not None:
            return self._staging_dir
        if self._published:
            return self.output_dir
        raise RuntimeError("run bundle workspace is not active")

    @property
    def path(self) -> Path:
        """Alias for ``root`` for callers that expect a filesystem path."""

        return self.root

    @property
    def staging_dir(self) -> Path:
        if self._staging_dir is None:
            raise RuntimeError("run bundle workspace has no active staging directory")
        return self._staging_dir

    @property
    def assets_dir(self) -> Path:
        return _workspace_path(self.root, self.artifacts.assets)

    @property
    def diagnostics_dir(self) -> Path:
        return _workspace_path(self.root, self.artifacts.diagnostics)

    def __enter__(self) -> RunBundleWorkspace:
        if self._staging_dir is not None or self._published:
            raise RuntimeError("run bundle workspace cannot be entered more than once")
        if self.output_dir.exists():
            raise RunBundleExistsError(f"run bundle already exists: {self.output_dir}")

        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = self.output_dir.parent / f".{self.output_dir.name}.staging-{uuid4().hex}"
        try:
            staging.mkdir()
            self._staging_dir = staging
            self._ensure_layout()
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            self._staging_dir = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Remove an unpublished workspace, including after interrupts."""

        if self._staging_dir is not None:
            shutil.rmtree(self._staging_dir, ignore_errors=True)
            self._staging_dir = None

    def stage_bundle(
        self,
        *,
        manifest: RunManifest,
        manual: ManualDocument,
        toc: list[TocEntry],
        validation: ValidationReport | None = None,
        replace: bool = False,
    ) -> Path:
        """Write canonical files into the active staging root.

        ``replace=True`` supports a validation pass over a completed draft and a
        subsequent final manifest/report without creating a second workspace.
        Only files previously written by this workspace may be replaced.
        """

        root = self.staging_dir
        _validate_bundle_inputs(manifest=manifest, manual=manual, validation=validation)
        self._validate_layout_compatibility(manifest.artifacts, replace=replace)
        self._ensure_layout()

        payloads: list[tuple[str, object]] = [
            (manifest.artifacts.manual, manual.model_dump(mode="json")),
            (manifest.artifacts.toc, [entry.model_dump(mode="json") for entry in toc]),
        ]
        if validation is not None and manifest.artifacts.validation is not None:
            payloads.append(
                (manifest.artifacts.validation, validation.model_dump(mode="json"))
            )
        payloads.append(("run.json", manifest.model_dump(mode="json")))

        targets = [(relative, _workspace_path(root, relative)) for relative, _ in payloads]
        for relative, target in targets:
            if target.exists() and (not replace or relative not in self._owned_paths):
                raise FileExistsError(
                    f"workspace file already exists and will not be overwritten: {relative}"
                )

        for (relative, value), (_, target) in zip(payloads, targets, strict=True):
            _write_json(target, value, overwrite=replace and relative in self._owned_paths)
            self._owned_paths.add(relative)
        self._staged_artifacts = manifest.artifacts
        return root

    def publish(self) -> Path:
        """Publish the staged bundle atomically without replacing a prior run."""

        root = self.staging_dir
        if self._staged_artifacts is None:
            raise RuntimeError("canonical bundle files must be staged before publication")
        self._ensure_layout()
        required = [
            "run.json",
            self._staged_artifacts.manual,
            self._staged_artifacts.toc,
        ]
        if self._staged_artifacts.validation is not None:
            required.append(self._staged_artifacts.validation)
        missing = [relative for relative in required if not _workspace_path(root, relative).is_file()]
        if missing:
            raise RuntimeError(f"cannot publish a bundle with missing files: {', '.join(missing)}")
        if self.output_dir.exists():
            raise RunBundleExistsError(f"run bundle already exists: {self.output_dir}")

        # Staging is adjacent, so the rename is confined to one filesystem.
        root.rename(self.output_dir)
        self._staging_dir = None
        self._published = True
        return self.output_dir

    def _ensure_layout(self) -> None:
        root = self.staging_dir
        assets_dir = _workspace_path(root, self.artifacts.assets)
        diagnostics_dir = _workspace_path(root, self.artifacts.diagnostics)
        for directory in (assets_dir, diagnostics_dir):
            if directory.exists() and not directory.is_dir():
                raise NotADirectoryError(f"workspace path must be a directory: {directory}")
            directory.mkdir(parents=True, exist_ok=True)
        for name in ("pages", "images", "tables"):
            child = assets_dir / name
            if child.exists() and not child.is_dir():
                raise NotADirectoryError(f"workspace path must be a directory: {child}")
            child.mkdir(exist_ok=True)

    def _validate_layout_compatibility(
        self,
        artifacts: ArtifactPaths,
        *,
        replace: bool,
    ) -> None:
        if artifacts.assets != self.artifacts.assets or artifacts.diagnostics != self.artifacts.diagnostics:
            raise ValueError(
                "manifest assets and diagnostics paths must match the workspace layout"
            )
        if replace and self._staged_artifacts is None:
            raise ValueError("replace=True requires a bundle previously staged by this workspace")
        if self._staged_artifacts is None:
            return
        if not replace:
            raise RuntimeError("bundle files are already staged; use replace=True to restage them")
        previous = self._staged_artifacts
        if artifacts.manual != previous.manual or artifacts.toc != previous.toc:
            raise ValueError("manual and toc artifact paths cannot change while restaging")
        if previous.validation is not None and artifacts.validation != previous.validation:
            raise ValueError("a staged validation artifact path cannot change")


def write_run_bundle(
    output_dir: Path,
    *,
    manifest: RunManifest,
    manual: ManualDocument,
    toc: list[TocEntry],
    validation: ValidationReport | None = None,
    workspace: RunBundleWorkspace | None = None,
) -> Path:
    """Write and atomically publish a complete run bundle.

    Existing callers can omit ``workspace``. New orchestrators can pass an
    already-open workspace populated by profiles, avoiding any asset copies.
    """

    resolved_output = output_dir.expanduser().resolve()
    if workspace is not None:
        if workspace.output_dir != resolved_output:
            raise ValueError("workspace output_dir must match write_run_bundle output_dir")
        workspace.stage_bundle(
            manifest=manifest,
            manual=manual,
            toc=toc,
            validation=validation,
        )
        return workspace.publish()

    with RunBundleWorkspace(resolved_output, artifacts=manifest.artifacts) as owned_workspace:
        owned_workspace.stage_bundle(
            manifest=manifest,
            manual=manual,
            toc=toc,
            validation=validation,
        )
        return owned_workspace.publish()


def _validate_bundle_inputs(
    *,
    manifest: RunManifest,
    manual: ManualDocument,
    validation: ValidationReport | None,
) -> None:
    if manifest.run_id != manual.id:
        raise ValueError("manifest.run_id must match manual.id")
    if validation is not None and validation.run_id != manifest.run_id:
        raise ValueError("validation.run_id must match manifest.run_id")
    declared_validation = manifest.artifacts.validation is not None
    if manifest.status is RunStatus.VALIDATED and validation is None:
        raise ValueError("a validated run bundle requires validation")
    if validation is not None and not declared_validation:
        raise ValueError("validation was supplied but artifacts.validation is not configured")
    if validation is None and declared_validation:
        raise ValueError("artifacts.validation is configured but validation was not supplied")
    if (
        validation is not None
        and validation.status == "failed"
        and manifest.status in {RunStatus.VALIDATED, RunStatus.EXPERIMENTAL}
    ):
        raise ValueError(
            f"a {manifest.status.value} run bundle requires a passed validation report"
        )


def _workspace_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise ValueError(f"artifact path escapes the workspace: {relative!r}") from error
    return candidate


def _write_json(path: Path, value: object, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if overwrite:
        path.write_text(serialized, encoding="utf-8")
        return
    with path.open("x", encoding="utf-8") as stream:
        stream.write(serialized)
