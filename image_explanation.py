# AIzaSyCZ93grMLfPnzsygsXrW9I54pDn40cIUmU
import os
import google.generativeai as genai
from PIL import Image

# Configure Gemini API
GOOGLE_API_KEY = "AIzaSyCZ93grMLfPnzsygsXrW9I54pDn40cIUmU"
genai.configure(api_key=GOOGLE_API_KEY)
def explain_image_with_gemini(image_path, explanations_dir):
    """Generate a concise, expert-level explanation for an image using Gemini and save it as a text file."""
    try:
        # Ensure the explanations directory exists
        if not os.path.exists(explanations_dir):
            os.makedirs(explanations_dir)

        # Load the image
        image = Image.open(image_path)

        # Initialize the Gemini model
        model = genai.GenerativeModel('gemini-2.0-flash')

        # Generate explanation with a precise, focused prompt
        response = model.generate_content(
            ["You are expert data analyst who can explain reports and narrate it also given calculated opinion on how to imporve the performance. `Only 2 line explanation`", image]
        )
        explanation = response.text.strip()

        # Define the output text file path
        base_filename = os.path.splitext(os.path.basename(image_path))[0]
        output_text_path = os.path.join(explanations_dir, f"{base_filename}_explanation.txt")

        # Save the explanation to a text file
        with open(output_text_path, 'w') as f:
            f.write(explanation)

        print(f"Explanation saved to {output_text_path}")
        return explanation
    except Exception as e:
        print(f"Error explaining image: {str(e)}")
        raise