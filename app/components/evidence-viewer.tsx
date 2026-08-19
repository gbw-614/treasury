import { Eye, Image as ImageIcon, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

export type EvidenceBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type EvidencePanel = {
  id: string;
  file: string;
  url: string;
  width: number;
  height: number;
  format: string;
};

export type EvidenceRegion = {
  id: string;
  panelId: string;
  text: string;
  boxes: EvidenceBox[];
  source?: "vision" | "ocr" | "reference";
};

type EvidenceViewerProps = {
  panels: EvidencePanel[];
  evidence: EvidenceRegion[];
  activeEvidenceIds: string[];
  pinnedEvidenceIds?: string[];
  panelIndex: number;
  onPanelIndexChange: (index: number) => void;
  onClearEvidence?: () => void;
  resetKey?: string;
  ariaLabel: string;
  imageAlt: (panelIndex: number) => string;
  emptyHint?: string;
  toolbarTitle?: string;
  minimalChrome?: boolean;
};

type Bounds = { left: number; top: number; right: number; bottom: number };

function fitMetrics(stageWidth: number, stageHeight: number, panel: EvidencePanel) {
  const availableWidth = Math.max(stageWidth - 52, 160);
  const availableHeight = Math.max(stageHeight - 52, 160);
  const fitScale = Math.min(
    availableWidth / panel.width,
    availableHeight / panel.height,
    1,
  );
  return { availableWidth, availableHeight, fitScale };
}

function boundsFor(regions: EvidenceRegion[]): Bounds | null {
  const boxes = regions.flatMap((region) => region.boxes);
  if (!boxes.length) return null;
  return boxes.reduce<Bounds>(
    (bounds, box) => ({
      left: Math.min(bounds.left, box.x),
      top: Math.min(bounds.top, box.y),
      right: Math.max(bounds.right, box.x + box.width),
      bottom: Math.max(bounds.bottom, box.y + box.height),
    }),
    { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity },
  );
}

export default function EvidenceViewer({
  panels,
  evidence,
  activeEvidenceIds,
  pinnedEvidenceIds = [],
  panelIndex,
  onPanelIndexChange,
  onClearEvidence,
  resetKey,
  ariaLabel,
  imageAlt,
  emptyHint = "Hover a result to zoom and center · click a result to pin",
  toolbarTitle,
  minimalChrome = false,
}: EvidenceViewerProps) {
  const [zoom, setZoom] = useState(1);
  const [stageSize, setStageSize] = useState({ width: 900, height: 650 });
  const imageStageRef = useRef<HTMLDivElement>(null);
  const imageSurfaceRef = useRef<HTMLDivElement>(null);
  const centerRequestRef = useRef(0);
  const safePanelIndex = Math.min(Math.max(panelIndex, 0), Math.max(0, panels.length - 1));
  const currentPanel = panels[safePanelIndex];
  const evidenceById = useMemo(
    () => new Map(evidence.map((region) => [region.id, region])),
    [evidence],
  );
  const activeRegions = activeEvidenceIds
    .map((id) => evidenceById.get(id))
    .filter((region): region is EvidenceRegion => Boolean(region));
  const currentRegions = currentPanel
    ? activeRegions.filter((region) => region.panelId === currentPanel.id)
    : [];
  const highlightedPanels = new Map<string, number>();
  for (const region of activeRegions) {
    highlightedPanels.set(region.panelId, (highlightedPanels.get(region.panelId) ?? 0) + 1);
  }

  useEffect(() => {
    centerRequestRef.current += 1;
    const frame = requestAnimationFrame(() => setZoom(1));
    return () => cancelAnimationFrame(frame);
  }, [resetKey]);

  useEffect(() => {
    const stage = imageStageRef.current;
    if (!stage) return;
    const measure = () => setStageSize({ width: stage.clientWidth, height: stage.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [resetKey]);

  useEffect(() => {
    if (!activeEvidenceIds.length) {
      centerRequestRef.current += 1;
      const frame = requestAnimationFrame(() => {
        setZoom(1);
        imageStageRef.current?.scrollTo({ left: 0, top: 0, behavior: "smooth" });
      });
      return () => cancelAnimationFrame(frame);
    }
    if (!panels.length) return;
    const firstRegion = activeEvidenceIds.map((id) => evidenceById.get(id)).find(Boolean);
    if (!firstRegion) return;
    const targetPanelIndex = panels.findIndex((panel) => panel.id === firstRegion.panelId);
    if (targetPanelIndex < 0) return;
    const targetPanel = panels[targetPanelIndex];
    const targetRegions = activeEvidenceIds
      .map((id) => evidenceById.get(id))
      .filter(
        (region): region is EvidenceRegion =>
          Boolean(region && region.panelId === targetPanel.id && region.boxes.length),
      );
    const bounds = boundsFor(targetRegions);
    if (!bounds) return;

    const { availableWidth, availableHeight, fitScale } = fitMetrics(stageSize.width, stageSize.height, targetPanel);
    const boundsWidth = Math.max(bounds.right - bounds.left, 1);
    const boundsHeight = Math.max(bounds.bottom - bounds.top, 1);
    const targetZoom = Math.max(
      1,
      Math.min(
        8,
        (availableWidth * 0.76) / (boundsWidth * fitScale),
        (availableHeight * 0.52) / (boundsHeight * fitScale),
      ),
    );
    const requestId = ++centerRequestRef.current;

    const centerSelection = () => {
      if (centerRequestRef.current !== requestId) return;
      const stage = imageStageRef.current;
      const surface = imageSurfaceRef.current;
      if (!stage || !surface || surface.dataset.panelId !== targetPanel.id) return;
      const centerX =
        surface.offsetLeft +
        (((bounds.left + bounds.right) / 2 / targetPanel.width) * surface.clientWidth);
      const centerY =
        surface.offsetTop +
        (((bounds.top + bounds.bottom) / 2 / targetPanel.height) * surface.clientHeight);
      stage.scrollTo({
        left: Math.max(0, centerX - stage.clientWidth / 2),
        top: Math.max(0, centerY - stage.clientHeight / 2),
        behavior: "smooth",
      });
    };
    const frame = requestAnimationFrame(() => {
      onPanelIndexChange(targetPanelIndex);
      setZoom(targetZoom);
      requestAnimationFrame(centerSelection);
    });
    const retry = window.setTimeout(centerSelection, 180);
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(retry);
    };
  }, [activeEvidenceIds, evidenceById, onPanelIndexChange, panels, stageSize.height, stageSize.width]);

  if (!currentPanel) {
    return <section className="artwork-pane evidence-viewer-empty" aria-label={ariaLabel}>No artwork loaded.</section>;
  }

  const { fitScale } = fitMetrics(stageSize.width, stageSize.height, currentPanel);
  const surfaceWidth = Math.max(1, Math.round(currentPanel.width * fitScale * zoom));
  const surfaceHeight = Math.max(1, Math.round(currentPanel.height * fitScale * zoom));

  return (
    <section className={`artwork-pane ${minimalChrome ? "minimal-chrome" : ""}`} aria-label={ariaLabel}>
      <div className="pane-toolbar">
        {minimalChrome ? (
          <>
            <strong className="viewer-minimal-title">{toolbarTitle ?? "Artwork"}</strong>
            {panels.length > 1 && (
              <div className="viewer-panel-picker" aria-label="Artwork panels">
                {panels.map((panel, index) => (
                  <button
                    key={panel.id}
                    className={index === safePanelIndex ? "active" : ""}
                    title={panel.file}
                    type="button"
                    onClick={() => onPanelIndexChange(index)}
                  >
                    <img src={panel.url} alt="" />
                    <span>{index + 1}</span>
                  </button>
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            <div className="panel-tabs">
              {toolbarTitle && <strong>{toolbarTitle}</strong>}
              <ImageIcon size={15} />
              {panels.map((panel, index) => (
                <button
                  key={panel.id}
                  className={index === safePanelIndex ? "active" : ""}
                  onClick={() => onPanelIndexChange(index)}
                  type="button"
                >
                  Panel {index + 1}
                  {Boolean(highlightedPanels.get(panel.id)) && <em>{highlightedPanels.get(panel.id)}</em>}
                </button>
              ))}
            </div>
            <div className="zoom-tools">
              {activeEvidenceIds.length > 0 && onClearEvidence && (
                <button
                  className="gt-clear-highlight"
                  onClick={onClearEvidence}
                  title="Clear text highlight"
                  type="button"
                >
                  <Eye size={15} />
                </button>
              )}
              <button onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))} title="Zoom out" type="button">
                <ZoomOut size={16} />
              </button>
              <span>{Math.round(zoom * 100)}%</span>
              <button onClick={() => setZoom((value) => Math.min(8, value + 0.25))} title="Zoom in" type="button">
                <ZoomIn size={16} />
              </button>
              <button onClick={() => setZoom(1)} title="Reset zoom" type="button">
                <RotateCcw size={15} />
              </button>
            </div>
          </>
        )}
      </div>
      <div
        className="image-stage gt-image-stage"
        ref={imageStageRef}
        onDoubleClick={() => setZoom(zoom === 1 ? 2.5 : 1)}
      >
        <div
          className="gt-image-surface"
          data-panel-id={currentPanel.id}
          ref={imageSurfaceRef}
          style={{ width: surfaceWidth, height: surfaceHeight }}
        >
          {/* Original-resolution evidence must remain unoptimized for trustworthy geometry. */}
          <img src={currentPanel.url} alt={imageAlt(safePanelIndex)} />
          {currentRegions.flatMap((region, regionIndex) =>
            region.boxes.map((box, boxIndex) => (
              <span
                key={`${region.id}-${boxIndex}`}
                className={`gt-image-highlight source-${region.source ?? "reference"} ${pinnedEvidenceIds.includes(region.id) ? "pinned" : ""}`}
                style={{
                  left: `${(box.x / currentPanel.width) * 100}%`,
                  top: `${(box.y / currentPanel.height) * 100}%`,
                  width: `${(box.width / currentPanel.width) * 100}%`,
                  height: `${(box.height / currentPanel.height) * 100}%`,
                }}
                title={`${region.text} · ${region.source === "vision" ? "Gemini geometry" : region.source === "ocr" ? "Tesseract geometry" : "reference geometry"}`}
              >
                {boxIndex === 0 && (
                  <b>
                    {region.source === "vision" ? "G" : region.source === "ocr" ? "T" : regionIndex + 1}
                  </b>
                )}
              </span>
            )),
          )}
        </div>
        {!minimalChrome && !activeEvidenceIds.length && (
          <div className="gt-image-hint"><Eye size={13} /> {emptyHint}</div>
        )}
      </div>
      {!minimalChrome && (
        <div className="image-caption">
          <span>{currentPanel.file}</span>
          <span>{currentPanel.width} × {currentPanel.height} · {currentPanel.format.toUpperCase()}</span>
        </div>
      )}
    </section>
  );
}
