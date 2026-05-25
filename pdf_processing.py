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
    from pptx.util import Emu

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
    sw, sh = prs.slide_width, prs.slide_height
    slide_aspect = sw / sh

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
    print(f"PPTX report saved: {output_pptx} ({len(images)} slides)")
    return output_pptx
