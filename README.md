# Anime Clip Maker

A responsive web app for uploading an anime episode, removing manually selected opening/ending time ranges, and splitting the remaining video into clips.

## Features
- Works in a mobile browser, including Android Chrome.
- Accepts common video formats supported by FFmpeg (including MKV and MP4).
- Optional OP and ED removal by entering time ranges in seconds.
- Custom clip length (default 90 seconds).
- Export MP4, MKV, or both in one ZIP.
- Keeps the final shorter clip when the remaining duration is not an exact multiple.

## Important limitation
**Automatic AI/audio/visual OP/ED detection is not implemented in this version.** You must enter the opening and ending time ranges yourself. The interface makes this explicit rather than pretending detection is automatic.

## Run locally
Install Python 3.11+ and FFmpeg/FFprobe, then:

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000.

## Deploy
The Dockerfile installs FFmpeg. The included Render blueprint is only a deployment starting point. Free hosting plans can have strict CPU, RAM, disk, request-duration and upload limits. Processing a 25-minute HD episode may exceed free-tier limits. Check the current host limits before uploading large videos.

## Output quality
For broad compatibility, clips are re-encoded using H.264 video and AAC audio. Processing can take time and output is not guaranteed to be bit-for-bit identical to the source.

## Security and production notes
This starter is intended for personal testing. Before making it public, add authentication, rate limiting, storage quotas, stronger upload validation, and background job handling. Do not upload content unless you have the rights to process it.
