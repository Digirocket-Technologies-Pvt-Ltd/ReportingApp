import fitz  # PyMuPDF
import os

def convert_pdf_to_images(pdf_path, output_dir):
    """Convert each page of a PDF to an image and save it in the specified directory."""
    try:
        # Open the PDF document
        pdf_document = fitz.open(pdf_path)

        # Ensure the output directory exists
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # Convert each page to an image
        for page_number in range(len(pdf_document)):
            page = pdf_document.load_page(page_number)
            pix = page.get_pixmap()
            img_filename = os.path.join(output_dir, f"page_{page_number + 1}.png")
            pix.save(img_filename)

        print(f"PDF converted to images and saved in {output_dir}")
    except Exception as e:
        print(f"Error converting PDF to images: {str(e)}")
        raise
