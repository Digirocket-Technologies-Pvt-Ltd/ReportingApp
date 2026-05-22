import os
from ai_vision import explain_image


def explain_image_with_gemini(image_path, explanations_dir):
    """Generate a concise, expert-level explanation for an image using Kimi AI and save it as a text file.

    (Function name kept for backwards compatibility; now powered by Kimi, not Gemini.)
    """
    try:
        # Ensure the explanations directory exists
        if not os.path.exists(explanations_dir):
            os.makedirs(explanations_dir)

        prompt = (
            "You are an expert data analyst who can explain reports and narrate them, "
            "and also give a calculated opinion on how to improve the performance. "
            "Only 2 line explanation."
        )
        explanation = explain_image(image_path, prompt)

        # Define the output text file path
        base_filename = os.path.splitext(os.path.basename(image_path))[0]
        output_text_path = os.path.join(explanations_dir, f"{base_filename}_explanation.txt")

        # Save the explanation to a text file
        with open(output_text_path, 'w', encoding='utf-8') as f:
            f.write(explanation)

        print(f"Explanation saved to {output_text_path}")
        return explanation
    except Exception as e:
        print(f"Error explaining image: {str(e)}")
        raise