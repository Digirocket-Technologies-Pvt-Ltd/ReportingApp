import os
from deepgram import DeepgramClient, SpeakOptions

DEEPGRAM_API_KEY = "dc7ec702a531c88144479248a00c382b71fd4b6f"

def convert_text_to_speech(text_file_path, output_audio_dir):
    """Convert text from a file to speech using Deepgram API and save it as an audio file."""
    try:
        # Ensure the output directory exists
        if not os.path.exists(output_audio_dir):
            os.makedirs(output_audio_dir)

        # Read the text from the file
        with open(text_file_path, 'r') as file:
            text = file.read()

        # Initialize Deepgram client
        deepgram = DeepgramClient(DEEPGRAM_API_KEY)

        # Set up speak options
        options = SpeakOptions(
            model="aura-arcas-en",
        )

        # Define the output audio file path
        base_filename = os.path.splitext(os.path.basename(text_file_path))[0]
        output_audio_path = os.path.join(output_audio_dir, f"{base_filename}.mp3")

        # Convert text to speech and save the audio file
        response = deepgram.speak.v("1").save(output_audio_path, {"text": text}, options)
        print(response.to_json(indent=4))

        print(f"Speech saved to {output_audio_path}")
    except Exception as e:
        print(f"Error converting text to speech: {str(e)}")
        raise
