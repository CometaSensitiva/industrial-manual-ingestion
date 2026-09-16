"""Generate a wholly synthetic manual, then run the real public pipeline.

Requires the docling extra and the fixed local Ollama setup. Never overwrites
an existing bundle. Run from the repository root: python examples/build_example.py
"""
from pathlib import Path
from io import BytesIO
import pymupdf
from PIL import Image, ImageDraw, ImageFont
from manual_ingestion.orchestrator import ingest_manual
from manual_ingestion.providers.ollama import OllamaCaptionProvider
from manual_ingestion.validation import validate_run_bundle, build_validation_report

ROOT = Path(__file__).resolve().parent
PDF = ROOT / "synthetic-manual.pdf"
BUNDLE = ROOT / "synthetic-bundle"

def make_source():
    image = Image.new("RGB", (1200, 720), "#f7f9f5")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    small = ImageFont.load_default(size=23)
    draw.rounded_rectangle((35, 35, 1165, 685), radius=25, fill="#e7ebe4", outline="#5c6c5e", width=4)
    draw.text((75, 62), "SYNTHETIC CONTROL PANEL - CP-01", fill="#263c2b", font=font)
    draw.rounded_rectangle((90, 140, 700, 570), radius=12, fill="#172c26")
    draw.text((125, 180), "SYSTEM STATUS", fill="#b8d6b0", font=font)
    draw.text((125, 270), "MODE: READY", fill="#ffffff", font=font)
    draw.text((125, 345), "SPEED: 120 rpm", fill="#ffffff", font=font)
    draw.text((125, 420), "GUARD: CLOSED", fill="#ffffff", font=font)
    draw.ellipse((820, 160, 990, 330), fill="#418951", outline="#245832", width=5)
    draw.text((858, 230), "START", fill="white", font=small)
    draw.ellipse((820, 380, 990, 550), fill="#b7352c", outline="#782821", width=5)
    draw.text((866, 450), "STOP", fill="white", font=small)
    draw.text((95, 610), "Illustrative equipment only - not operating instructions", fill="#52634f", font=small)
    buf=BytesIO(); image.save(buf, format="PNG")
    doc=pymupdf.open()
    for number in (1,2):
        page=doc.new_page(width=595,height=842)
        page.insert_text((48,45),"FIELD NOTES / SYNTHETIC EQUIPMENT",fontsize=9,color=(.35,.45,.35))
        page.insert_text((48,794),f"Public demonstration manual | {number} / 2",fontsize=9,color=(.4,.45,.4))
        if number == 1:
            page.insert_text((48,94),"1  Control panel",fontsize=24,color=(.15,.25,.18))
            page.insert_textbox(pymupdf.Rect(48,121,547,220),
                "The CP-01 is a fictional training unit. This document demonstrates how a technical manual can preserve text, images and tables in a structured bundle. The display and the two push buttons are shown in Figure 1. Read the original figure to verify any automatically generated description.",fontsize=12,lineheight=1.5)
            page.insert_image(pymupdf.Rect(48,260,547,560),stream=buf.getvalue())
            page.insert_text((48,586),"Figure 1. CP-01 operator panel.",fontsize=11)
            page.insert_textbox(pymupdf.Rect(48,630,547,740),"All names, values and graphics in this document were invented for a software demonstration. No real machine or proprietary manual is represented. The image contains information that is not repeated in the surrounding text.",fontsize=11,lineheight=1.5)
        else:
            page.insert_text((48,94),"2  Inspection schedule",fontsize=24,color=(.15,.25,.18))
            page.insert_textbox(pymupdf.Rect(48,121,547,210),"The following fictional schedule demonstrates structured table extraction. Each row connects a component to its inspection interval and method. The table is represented using header-value rows, without sending it to a vision model.",fontsize=12,lineheight=1.5)
            rows=[["Component","Interval","Method"],["Guard latch","Daily","Visual inspection"],["Drive belt","Monthly","Check tension"],["Air filter","Quarterly","Inspect and replace"]]
            xs=[48,218,348,547]; top=250; height=47
            for i,row in enumerate(rows):
                page.draw_rect(pymupdf.Rect(48,top+i*height,547,top+(i+1)*height),color=(.6,.65,.6),fill=(.9,.94,.88) if i==0 else None,width=.7)
                for j,text in enumerate(row):
                    page.insert_text((xs[j]+10,top+i*height+29),text,fontsize=11)
            for x in xs[1:-1]: page.draw_line((x,top),(x,top+len(rows)*height),color=(.6,.65,.6),width=.7)
            page.insert_textbox(pymupdf.Rect(48,485,547,610),"These intervals are synthetic examples, not maintenance advice. The structured representation preserves which method belongs to each component. Users can inspect the source page alongside the extracted table and its serialized rows.",fontsize=12,lineheight=1.5)
    doc.set_toc([[1,"1 Control panel",1],[1,"2 Inspection schedule",2]])
    doc.set_metadata({"title":"CP-01 · Demonstration manual","author":"Industrial Manual Ingestion"})
    doc.save(PDF)
    doc.close()


def main():
    if BUNDLE.exists(): raise SystemExit("Example bundle already exists. Move it aside before deliberately regenerating.")
    make_source()
    outcome=ingest_manual(PDF,BUNDLE,run_id="synthetic-example",provider=OllamaCaptionProvider(),language="en",title="CP-01 · Demonstration manual")
    pages=BUNDLE/"assets/pages"; pages.mkdir(exist_ok=True)
    with pymupdf.open(PDF) as doc:
        for i,page in enumerate(doc): page.get_pixmap(matrix=pymupdf.Matrix(1.5,1.5)).save(pages/f"page_{i+1:04d}.png")
    report=build_validation_report(BUNDLE)
    (BUNDLE/"validation.json").write_text(report.model_dump_json(indent=2)+"\n")
    report=validate_run_bundle(BUNDLE)
    if report.status != "passed":
        raise RuntimeError([(c.id,c.message) for c in report.checks if not c.passed])
    print(f"Example: {outcome.manifest.status.value}; validation: {report.status}")

if __name__ == "__main__": main()
