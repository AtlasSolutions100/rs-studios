# Light-model worker (Mac mini)

The campaign agent asks a few multiple-choice questions before it plans a new campaign. Those questions come from a
small local model on the Mac mini, so they cost no Claude API. Claude still does the heavy work: the plan itself,
the copy, the claims check.

How it works: `worker.py` keeps one request open to `https://campaignify.ai/_light/next`. When someone starts a
campaign, the site puts a job in a queue, the worker takes it, runs it on Ollama with the Mac's GPU, and posts the
answer back. If the Mac is off or slow, the site waits about 20 seconds at most, then falls back to Groq or to
simple built-in questions, so nothing breaks.

## Set it up (once)

1. Make the token on the Mac, and copy it:

   ```
   openssl rand -hex 32
   ```

2. Set it as a GitHub secret named `LIGHT_WORKER_TOKEN` in AtlasSolutions100/rs-studios (Settings, Secrets and
   variables, Actions), or from the Mac (it asks for the value):

   ```
   gh secret set LIGHT_WORKER_TOKEN --repo AtlasSolutions100/rs-studios
   ```

   The next deploy gives it to the site.

3. Get these files and run the setup. It installs Ollama with Homebrew, pulls the model (about 9 GB), asks for the
   token (it isn't shown) and starts the worker with launchd, so it comes back after a restart:

   ```
   git clone --depth 1 https://github.com/AtlasSolutions100/rs-studios ~/rs-studios
   bash ~/rs-studios/deploy/mac-light/setup.sh
   ```

## Check it

- `tail -f ~/campaignify-light/worker.log` shows each job and how long it took.
- The admin page's settings list shows "Mac worker on (qwen3:14b)" while the worker is connected.

## Change things

- Another model: `CFY_LIGHT_MODEL=qwen3:8b bash ~/rs-studios/deploy/mac-light/setup.sh` (faster, a little less
  sharp). The model and the site address live in `~/.campaignify-light.env`.
- Update the worker: `git -C ~/rs-studios pull` and run `setup.sh` again.
- Stop it: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.campaignify.light.plist`.

It runs beside the NeuraCut worker without trouble: that one runs in Docker, this one is a plain Python process, and
Ollama uses the GPU directly (Docker on a Mac can't).
