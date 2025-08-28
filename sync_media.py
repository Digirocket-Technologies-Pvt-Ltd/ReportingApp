from moviepy.editor import ImageSequenceClip, AudioFileClip, concatenate_videoclips
import os
import re
import time

def extract_page_number(filename):
    # Extract the page number from the filename using regex
    match = re.search(r'page_(\d+)', filename)
    return int(match.group(1)) if match else None

def create_video_from_images_and_audio(image_folder, audio_folder, output_video):
    print("Starting fade-only video creation process...")
    start_time = time.time()
    
    # Get list of image and audio files
    print("Scanning directories for image and audio files...")
    print(f"Looking in image folder: {image_folder}")
    print(f"Looking in audio folder: {audio_folder}")
    
    image_files = [f for f in os.listdir(image_folder) if f.endswith(('.png', '.jpg', '.jpeg'))]
    audio_files = [f for f in os.listdir(audio_folder) if f.endswith(('.mp3', '.wav'))]
    
    print(f"Found {len(image_files)} image files and {len(audio_files)} audio files")

    # Ensure there is at least one image and one audio file
    if not image_files or not audio_files:
        raise ValueError("No images or audio files found in the specified directories.")

    # Sort files based on the extracted page number
    print("Sorting files by page number...")
    image_files.sort(key=lambda x: extract_page_number(x))
    audio_files.sort(key=lambda x: extract_page_number(x))

    # Create a list to hold video clips
    video_clips = []

    # Loop through images and corresponding audio files
    print("\nStarting to process individual slides:")
    for i, (image_file, audio_file) in enumerate(zip(image_files, audio_files)):
        print(f"Processing slide {i+1}/{len(image_files)}: {image_file}")
        
        image_path = os.path.join(image_folder, image_file)
        audio_path = os.path.join(audio_folder, audio_file)

        print(f"  Loading image: {image_path}")
        # Load the image and audio
        image_clip = ImageSequenceClip([image_path], durations=[5])
        
        print(f"  Loading audio: {audio_path}")
        audio_clip = AudioFileClip(audio_path)
        print(f"  Audio duration: {audio_clip.duration:.2f} seconds")

        # Set the duration of the image clip to match the audio clip
        print("  Setting image duration to match audio...")
        image_clip = image_clip.set_duration(audio_clip.duration)
        
        # Only add fade effects
        print("  Adding fade effects...")
        image_clip = image_clip.fadein(0.5).fadeout(0.5)
        
        # Combine the image and audio into a video clip
        print("  Combining image and audio...")
        video_clip = image_clip.set_audio(audio_clip)
        
        video_clips.append(video_clip)
        print(f"  Completed processing slide {i+1}\n")

    # Concatenate all video clips into a single video
    print("Concatenating all video clips...")
    final_video = concatenate_videoclips(video_clips, method="compose")
    
    # Calculate total duration
    total_duration = sum(clip.duration for clip in video_clips)
    print(f"Total video duration: {total_duration:.2f} seconds")

    # Write the final video to a file
    print(f"Writing final video to {output_video}...")
    final_video.write_videofile(output_video, codec='libx264', fps=24)
    
    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Fade-only video creation completed in {elapsed_time:.2f} seconds")

if __name__ == "__main__":
    print("Starting video creation script...")
    print(f"Current working directory: {os.getcwd()}")
    
    # Fix the paths to be relative to the current directory instead of including 'app/'
    image_folder = "images/AIVideo"
    audio_folder = "images/AudioExplanations"
    output_video = "output_video_fade_only.mp4"
    
    print(f"Image folder: {image_folder}")
    print(f"Audio folder: {audio_folder}")
    print(f"Output video file: {output_video}")
    
    # Check if directories exist
    if not os.path.exists(image_folder):
        print(f"WARNING: Image folder '{image_folder}' does not exist!")
    if not os.path.exists(audio_folder):
        print(f"WARNING: Audio folder '{audio_folder}' does not exist!")
    
    try:
        create_video_from_images_and_audio(image_folder, audio_folder, output_video)
        print(f"Video with fade effects created successfully: {output_video}")
    except Exception as e:
        print(f"ERROR: An exception occurred during video creation: {str(e)}")
        import traceback
        print(traceback.format_exc())