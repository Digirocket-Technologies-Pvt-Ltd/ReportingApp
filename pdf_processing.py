import os
import re
from PyPDF2 import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import landscape, A4
from PIL import Image
import io
import fitz  # PyMuPDF - render each reportlab page straight to an image (no PyPDF2 merge)
from ai_vision import explain_image

font_heading = "Helvetica-Bold"
font_description = "Helvetica"
background_color = "#E7EDF1"

def clean_title(filename):
    """Clean the image title by removing numbers, underscores, and file extension."""
    # Remove file extension
    title = os.path.splitext(filename)[0]
    # Remove numbers and underscores from the beginning
    title = re.sub(r'^\d+[_-]*', '', title)
    # Replace remaining underscores with spaces and capitalize
    title = title.replace('_', ' ').strip().title()
    return title

def get_image_description(image_path):
    """Generate a one-line image description using Kimi AI."""
    try:
        return explain_image(image_path, "Generate a one-line explanation of this image.")
    except Exception as e:
        print(f"Error getting image description: {e}")
        return ""

def process_pdf(template_pdf_path, images_folder, output_pdf_path, start_page):
    """Process PDF and add images starting from specified page."""
    # Read existing PDF
    reader = PdfReader(template_pdf_path)
    writer = PdfWriter()

    # Copy pages up to start_page
    for i in range(start_page):
        writer.add_page(reader.pages[i])

    # Get sorted list of images
    images = sorted([f for f in os.listdir(images_folder)
                    if f.lower().endswith(('.png', '.jpg', '.jpeg'))])

    # Get template page for dimensions
    template_page = reader.pages[0]

    # Process each image
    for idx, image in enumerate(images, start=1):  # Start counting from 1
        image_path = os.path.join(images_folder, image)

        # Get image description
        description = get_image_description(image_path)

        # Create a new PDF in memory
        packet = io.BytesIO()
        # Pass the image index to determine width
        add_image_to_pdf(packet, image_path, description, template_page, idx)

        # Move to the beginning of the BytesIO buffer
        packet.seek(0)
        new_pdf = PdfReader(packet)

        # Add the new page
        writer.add_page(new_pdf.pages[0])

    # Copy remaining pages from template if any
    for i in range(start_page, len(reader.pages)):
        writer.add_page(reader.pages[i])

    # Write the final PDF
    with open(output_pdf_path, 'wb') as output_file:
        writer.write(output_file)

def add_image_to_pdf(packet, image_path, description, template_page, image_index):
    """Add image and description to a PDF page matching template dimensions."""
    # Get dimensions from template page
    template_width = float(template_page.mediabox.width)
    template_height = float(template_page.mediabox.height)

    # Create canvas with template dimensions
    can = canvas.Canvas(packet, pagesize=(template_width, template_height))

    # Add dotted background
    dot_radius = 1  # Radius of each dot
    dot_spacing = 20  # Space between dots (both horizontally and vertically)
    dot_color = Color(0.8, 0.8, 0.8, alpha=1)  # Light gray color for dots

    for x in range(0, int(template_width), dot_spacing):
        for y in range(0, int(template_height), dot_spacing):
            can.setFillColor(dot_color)
            can.circle(x, y, dot_radius, fill=True, stroke=False)

    # Add gradient background
    can.setFillColor(HexColor('#E7EDF1'))  # Light gray background
    can.rect(0, 0, template_width, template_height, fill=True)

    # Load and resize image while maintaining aspect ratio
    img = Image.open(image_path)
    img_width, img_height = img.size
    aspect = img_width / float(img_height)

    # Calculate new dimensions
    if image_index == 1 or image_index == 7:
        # Keep original size for 1st and 7th images
        new_height = template_height * 0.5
        new_width = new_height * aspect
        max_width_ratio = 0.9  # Constrain width to 90% of page width
    else:
        # Make other images larger and wider
        new_height = template_height * 0.6  # Increase height to 70% of page height
        new_width = new_height * aspect
        max_width_ratio = 1.0  # Allow width to be up to 90% of page width

    # Constrain width if it exceeds the maximum allowed width
    if new_width > template_width * max_width_ratio:
        new_width = template_width * max_width_ratio
        new_height = new_width / aspect  # Adjust height to maintain aspect ratio

    # Center the image
    x = (template_width - new_width) / 2
    y = (template_height - new_height) / 2

    # Draw image
    can.drawImage(image_path, x, y, width=new_width, height=new_height)

    # Add rounded rectangle border around the image
    border_thickness = 10  # Thickness of the border
    border_color = HexColor('#002060')  # Dark blue color for border
    corner_radius = 20  # Rounded corners

    # Draw the rounded rectangle border
    can.setStrokeColor(border_color)
    can.setLineWidth(border_thickness)
    can.roundRect(
        x - border_thickness / 2,
        y - border_thickness / 2,
        new_width + border_thickness,
        new_height + border_thickness,
        corner_radius,
        stroke=True,
        fill=False
    )

    # Get clean image title
    image_title = clean_title(os.path.basename(image_path))

    # Add title text above the image with gradient effect
    can.setFont("Helvetica-Bold", 40)
    title_width = can.stringWidth(image_title, "Helvetica-Bold", 40)
    title_x = (template_width - title_width) / 2
    title_y = y + new_height + 100

    # Gradient fill for title
    can.setFillColor(HexColor('#002060'))  # Start color
    can.drawString(title_x, title_y, image_title)

    # Add shadow effect for title
    can.setFillColor(colors.black)
    can.drawString(title_x + 2, title_y - 2, image_title)  # Shadow offset

    # Reset fill color back to black for other text
    can.setFillColor(colors.black)

    # Add description text below the image
    can.setFont("Helvetica", 18)
    description_lines = []

    # Split description into multiple lines if too long
    max_width = template_width * 0.8
    while can.stringWidth(description, "Helvetica", 18) > max_width:
        words = description.split()
        current_line = []
        while words and can.stringWidth(' '.join(current_line + [words[0]]), "Helvetica", 18) <= max_width:
            current_line.append(words.pop(0))
        description_lines.append(' '.join(current_line))
        description = ' '.join(words)
    if description:
        description_lines.append(description)

    # Draw description lines with padding and spacing
    text_y = y - 100
    for line in description_lines:
        text_width = can.stringWidth(line, "Helvetica", 18)
        text_x = (template_width - text_width) / 2
        can.drawString(text_x, text_y, line)
        text_y -= 30  # Increased spacing between lines

    can.save()


def build_slide_images(template_pdf_path, images_folder, output_dir, start_page, zoom=2):
    """Build the slide images for the video WITHOUT merging PDFs.

    The old approach merged many reportlab pages into one PDF with PyPDF2, which
    dropped image XObjects on some pages (-> blank slides + 'cannot find XObject'
    MuPDF errors). Here each reportlab page is rendered straight to an image with
    PyMuPDF, so nothing is merged and no image is ever lost. Also faster.

    Output: page_1.png, page_2.png, ... in `output_dir` (HD via `zoom`).
    """
    os.makedirs(output_dir, exist_ok=True)
    template = fitz.open(template_pdf_path)
    template_page_ref = PdfReader(template_pdf_path).pages[0]  # only for dimensions
    matrix = fitz.Matrix(zoom, zoom)
    page_num = 0

    def save_pixmap(pix):
        nonlocal page_num
        page_num += 1
        pix.save(os.path.join(output_dir, f"page_{page_num}.png"))

    # 1) Template cover pages (rendered directly from the template - no merge)
    cover_count = min(start_page, len(template))
    for i in range(cover_count):
        save_pixmap(template[i].get_pixmap(matrix=matrix))

    # 2) Data slides: build each reportlab page, render it straight to an image
    images = sorted([f for f in os.listdir(images_folder)
                     if f.lower().endswith(('.png', '.jpg', '.jpeg'))])
    for idx, image in enumerate(images, start=1):
        image_path = os.path.join(images_folder, image)
        try:
            description = get_image_description(image_path)
        except Exception as e:
            print(f"  (description failed for {image}: {e})")
            description = ""
        packet = io.BytesIO()
        add_image_to_pdf(packet, image_path, description, template_page_ref, idx)
        packet.seek(0)
        single = fitz.open(stream=packet.read(), filetype='pdf')
        save_pixmap(single[0].get_pixmap(matrix=matrix))
        single.close()

    # 3) Remaining template pages (Thank You etc.)
    for i in range(start_page, len(template)):
        save_pixmap(template[i].get_pixmap(matrix=matrix))

    template.close()
    print(f"Built {page_num} slide images in {output_dir} (no-merge, HD)")
    return page_num


def build_pdf_from_images(images_dir, output_pdf):
    """Combine the slide images (page_1.png ...) into one downloadable PDF report.

    Uses PyMuPDF to insert each image as a page - reliable, no XObject merge bug.
    """
    def _page_num(name):
        m = re.search(r'page_(\d+)', name)
        return int(m.group(1)) if m else 0

    images = sorted(
        [f for f in os.listdir(images_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))],
        key=_page_num,
    )
    doc = fitz.open()
    for name in images:
        imgdoc = fitz.open(os.path.join(images_dir, name))
        pdfbytes = imgdoc.convert_to_pdf()
        imgdoc.close()
        imgpdf = fitz.open("pdf", pdfbytes)
        doc.insert_pdf(imgpdf)
        imgpdf.close()
    doc.save(output_pdf)
    doc.close()
    print(f"PDF report saved: {output_pdf} ({len(images)} pages)")
    return output_pdf


def build_pptx_from_images(images_dir, output_pptx):
    """Combine the slide images (page_1.png ...) into a downloadable PPTX deck.

    Each slide is one full-slide image (the same images used for the PDF), so the
    deck matches the report exactly. The PMO team can then open it in PowerPoint
    to reorder/remove slides, add their own slides, or add speaker notes.
    """
    from pptx import Presentation
    from pptx.util import Emu, Inches, Pt
    from pptx.dml.color import RGBColor

    def _page_num(name):
        m = re.search(r'page_(\d+)', name)
        return int(m.group(1)) if m else 0

    images = sorted(
        [f for f in os.listdir(images_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))],
        key=_page_num,
    )

    prs = Presentation()
    prs.slide_width = Emu(12192000)   # 13.333in -> 16:9 widescreen
    prs.slide_height = Emu(6858000)   # 7.5in
    blank_layout = prs.slide_layouts[6]
    sw, sh = int(prs.slide_width), int(prs.slide_height)
    slide_aspect = sw / float(sh)

    for name in images:
        path = os.path.join(images_dir, name)
        slide = prs.slides.add_slide(blank_layout)
        with Image.open(path) as im:
            iw, ih = im.size
        img_aspect = iw / float(ih)
        # Fit the image inside the slide, preserving aspect ratio, centered.
        if img_aspect > slide_aspect:
            w = sw
            h = int(sw / img_aspect)
        else:
            h = sh
            w = int(sh * img_aspect)
        left = int((sw - w) / 2)
        top = int((sh - h) / 2)
        slide.shapes.add_picture(path, left, top, width=w, height=h)

    prs.save(output_pptx)
    print(f"PPTX (image slides) saved: {output_pptx} ({len(images)} slides)")
    return output_pptx


def build_editable_pptx(context, output_pptx, slide_images=None):
    """Build a PowerPoint report.

    If `slide_images` (list of page_N.png paths) is given, those are added first
    as full-slide images so the deck VISUALLY matches the PDF (cover, colours,
    charts, screenshots). Then native EDITABLE slides (overview metrics + data
    tables) are appended so the PMO team can still edit the numbers/text.

    context = {
        'title': str, 'subtitle': str,
        'metrics': [(label, value), ...],
        'tables': [{'title': str, 'headers': [...], 'rows': [[...], ...]}, ...],
    }
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.enum.shapes import MSO_SHAPE

    NAVY = RGBColor(0x00, 0x20, 0x60)
    LIME = RGBColor(0x6F, 0xA8, 0x1F)
    DARK = RGBColor(0x1F, 0x29, 0x37)
    GREY = RGBColor(0x6B, 0x72, 0x80)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)
    LIGHT = RGBColor(0xF1, 0xF5, 0xF9)

    prs = Presentation()
    prs.slide_width = Emu(12192000)   # 13.333in -> 16:9
    prs.slide_height = Emu(6858000)   # 7.5in
    blank = prs.slide_layouts[6]
    SW, SH = int(prs.slide_width), int(prs.slide_height)

    def title_bar(slide, text):
        box = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), SW - Inches(1), Inches(0.9))
        tf = box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = text
        p.font.size = Pt(24)
        p.font.bold = True
        p.font.color.rgb = NAVY

    # ---- If slide images are given, the deck is JUST those (matches the PDF
    #      exactly). No duplicated "Editable Data" tables. ----
    if slide_images:
        slide_aspect = SW / float(SH)
        for path in slide_images:
            if not os.path.exists(path):
                continue
            sl = prs.slides.add_slide(blank)
            with Image.open(path) as im:
                iw, ih = im.size
            img_aspect = iw / float(ih)
            if img_aspect > slide_aspect:
                w = SW; h = int(SW / img_aspect)
            else:
                h = SH; w = int(SH * img_aspect)
            sl.shapes.add_picture(path, int((SW - w) / 2), int((SH - h) / 2), width=w, height=h)
        prs.save(output_pptx)
        print(f"PPTX (image slides) saved: {output_pptx}")
        return output_pptx

    # ---- Otherwise build a native editable deck (title + metrics + tables) ----
    s = prs.slides.add_slide(blank)
    tb = s.shapes.add_textbox(Inches(0.7), Inches(2.4), SW - Inches(1.4), Inches(2))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = context.get('title', 'Analytics Report')
    p.font.size = Pt(44)
    p.font.bold = True
    p.font.color.rgb = NAVY
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = context.get('subtitle', '')
    p2.font.size = Pt(20)
    p2.font.color.rgb = GREY
    p2.alignment = PP_ALIGN.CENTER

    # ---- Overview metrics slide ----
    metrics = context.get('metrics') or []
    if metrics:
        s = prs.slides.add_slide(blank)
        title_bar(s, 'Overview')
        per_row = 4
        gap = Inches(0.3)
        left0 = Inches(0.5)
        card_w = int((SW - Inches(1) - gap * (per_row - 1)) / per_row)
        card_h = Inches(1.9)
        GREENISH = RGBColor(0x86, 0xEF, 0xAC)
        REDDISH = RGBColor(0xFE, 0xCA, 0xCA)
        for i, m in enumerate(metrics):
            label, value = m[0], m[1]
            change = m[2] if len(m) > 2 else None
            row = i // per_row
            col = i % per_row
            x = int(left0 + col * (card_w + gap))
            top = int(Inches(1.7) + row * (card_h + Inches(0.35)))
            shp = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, top, card_w, card_h)
            shp.fill.solid()
            shp.fill.fore_color.rgb = DARK
            shp.line.color.rgb = DARK
            tf = shp.text_frame
            tf.word_wrap = True
            pv = tf.paragraphs[0]
            pv.text = str(value)
            pv.font.size = Pt(28)
            pv.font.bold = True
            pv.font.color.rgb = LIME
            pv.alignment = PP_ALIGN.CENTER
            pl = tf.add_paragraph()
            pl.text = label
            pl.font.size = Pt(12)
            pl.font.color.rgb = WHITE
            pl.alignment = PP_ALIGN.CENTER
            if change is not None:
                pc = tf.add_paragraph()
                pc.text = ('▲ ' if change >= 0 else '▼ ') + str(abs(change)) + '% vs prev'
                pc.font.size = Pt(10)
                pc.font.bold = True
                pc.font.color.rgb = GREENISH if change >= 0 else REDDISH
                pc.alignment = PP_ALIGN.CENTER

    # ---- Native editable charts (infographics) ----
    charts = context.get('charts') or []
    if charts:
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        for ch in charts:
            cats = [str(c) for c in (ch.get('categories') or [])]
            vals = [float(v) for v in (ch.get('values') or [])]
            if not cats or not vals:
                continue
            s = prs.slides.add_slide(blank)
            title_bar(s, ch.get('title', ''))
            cd = CategoryChartData()
            cd.categories = cats
            cd.add_series('Value', vals)
            ctype = XL_CHART_TYPE.LINE if ch.get('kind') == 'line' else XL_CHART_TYPE.COLUMN_CLUSTERED
            gframe = s.shapes.add_chart(ctype, Inches(0.6), Inches(1.5),
                                        SW - Inches(1.2), SH - Inches(2.2), cd)
            try:
                gframe.chart.has_legend = False
            except Exception:
                pass

    # ---- One slide per table ----
    for t in context.get('tables', []):
        headers = t.get('headers') or []
        rows = (t.get('rows') or [])[:14]
        if not headers or not rows:
            continue
        s = prs.slides.add_slide(blank)
        title_bar(s, t.get('title', ''))
        nrows = len(rows) + 1
        ncols = len(headers)
        left = Inches(0.5)
        top = Inches(1.4)
        width = SW - Inches(1)
        height = min(SH - Inches(1.7), Inches(0.4) * nrows)
        table = s.shapes.add_table(nrows, ncols, left, top, width, int(height)).table
        for c, h in enumerate(headers):
            cell = table.cell(0, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = NAVY
            cp = cell.text_frame.paragraphs[0]
            cp.text = str(h)
            cp.font.size = Pt(11)
            cp.font.bold = True
            cp.font.color.rgb = WHITE
        for r, row in enumerate(rows, start=1):
            for c in range(ncols):
                cell = table.cell(r, c)
                cell.fill.solid()
                cell.fill.fore_color.rgb = WHITE if r % 2 else LIGHT
                cp = cell.text_frame.paragraphs[0]
                cp.text = str(row[c]) if c < len(row) else ''
                cp.font.size = Pt(10)
                cp.font.color.rgb = DARK

    prs.save(output_pptx)
    print(f"Editable PPTX saved: {output_pptx}")
    return output_pptx
