# In-app voice agent example

Minimal client for embedding Ozzy in another product. It clones a voice, plays an audition, and speaks a reply script. Bring your own STT and LLM.

```sh
export VOICE_ORIGIN=https://127.0.0.1:8765
export VOICE_API_KEY=your-integration-key
python client.py clone ./authorized-reference.wav "Agent voice"
python client.py speak "$VOICE_ID" "Thanks for calling. How can I help today?"
```

`client.py` uses only the Python standard library. Install Chatterbox **Original** on the studio before cloning for production-quality speech:

```sh
uv run --no-sync voice models install original
```
