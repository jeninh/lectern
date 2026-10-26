# Offline mode

One setting that guarantees Lectern makes no outbound internet connections. (Not to be confused
with the installed app keeping books on your phone, which is [offline.md](offline.md).)

Readers, e-readers, the web app, and workers still reach Lectern as before: offline mode is
about what the *server* starts, not what connects to it.

## What contacts the internet otherwise

Admin → Offline mode lists these with whether each is on right now.

![Admin → Offline mode](screenshots/offline-mode-admin.png)

| Feature | Contacts | When |
|---|---|---|
| Metadata suggestions (off by default) | `openlibrary.org`, `www.googleapis.com` | Someone asks for suggestions in the book editor: the ISBN, or title and first author |
| ElevenLabs narration (only with your key) | `api.elevenlabs.io` | Testing the key, voices, previews, estimates, and narrations you confirmed |
| Alignment model download (`align` image) | `huggingface.co` | Once, at the server's first alignment, unless the model is already downloaded |
| Kokoro download (`LECTERN_TTS_KOKORO_LOCAL=1`) | `huggingface.co` | Once, at the server's first Kokoro narration, unless already downloaded |

Nothing else in the server opens a connection outward. Not on that list, because they are not
the server: a Mac worker downloads its own models, and `/worker/install.sh` makes the Mac
fetch the worker from GitHub and PyPI. Restic backups write to a local folder.

## Turning it on

Either:

- **Admin → Offline mode**, the switch (owner only). It takes effect at once and survives
  restarts.
- **`LECTERN_OFFLINE=1`** in the server's environment (`.env` for the Tailscale setup). It is
  then on from the first moment the server runs, and Admin cannot turn it off.

While it is on:

- every outbound connection from the server is refused at one choke point, whatever asks
  for it, with the error *Lectern is in offline mode: refused to contact …*. Name lookups are
  refused too (DNS is outbound traffic); loopback (the reverse proxy, `tailscale serve`, the
  health check) keeps working;
- metadata suggestions and ElevenLabs show as unavailable, with that reason, and send
  nothing: a queued ElevenLabs narration fails at its next request instead of retrying;
- alignment loads its model from the data directory only (`HF_HUB_OFFLINE=1`, local files
  only). A missing model fails the job with a message that says to download it first.

## Download models first

Offline mode only blocks downloads; it cannot make up for them. Before turning it on, download
what the server needs to align and narrate:

- **Admin → Offline mode → Download models now**, or
- on the host:

  ```sh
  docker compose --env-file .env -f deploy/tailscale/compose.yaml exec lectern lectern prepare-models
  ```

By default that downloads the configured alignment model (`LECTERN_ALIGN_MODEL`) when the
image has the `align` extra, Kokoro's weights and voices when `LECTERN_TTS_KOKORO_LOCAL=1`,
and one Piper voice (`en_US-lessac-medium`) when Piper is installed without any voice. Choose
others with `--align-model base`, `--piper-voice de_DE-thorsten-high` (both repeatable), and
`--kokoro`. Models go into the data volume: alignment models into `<data>/models`, Kokoro into
the Hugging Face cache (`HF_HOME=/data/huggingface` in the image), Piper voices into
`<data>/tts/piper`. The download refuses to run while offline mode is on; when
`LECTERN_OFFLINE=1` set it, run that one command with `-e LECTERN_OFFLINE=0` after `exec`.

A job that asks for a different alignment model than the one downloaded fails while offline;
download that model too.

## A hard guarantee: firewall rules

Offline mode is enforced inside Lectern's Python processes. Programs Lectern runs that are not
Python (ffmpeg, restic) and anything else in the container are outside it. Firewall rules in
the container's network namespace close that gap: new outbound connections are rejected,
while answers to incoming connections and loopback still work. They use `nft` on the host
(`sudo apt install nftables` if it is missing) and last until the container is recreated, so
run them again after every `docker compose … up` that recreates it.

**Tailscale setup** (`deploy/tailscale/compose.yaml`). Lectern shares the Tailscale
container's network, and Tailscale itself needs the internet to stay connected, so the rules
match Lectern's user and leave Tailscale's traffic alone. Replace `1000` if you set another
`LECTERN_UID` in `.env`:

```sh
PID=$(docker inspect -f '{{.State.Pid}}' "$(docker compose --env-file .env -f deploy/tailscale/compose.yaml ps -q tailscale)")
sudo nsenter -t "$PID" -n nft -f - <<'EOF'
table inet lectern_offline {
  chain output {
    type filter hook output priority 0; policy accept;
    meta skuid 1000 oif "lo" accept
    meta skuid 1000 ct state established,related accept
    meta skuid 1000 reject
  }
}
EOF
```

**Plain `compose.yaml`** (a reverse proxy on the host reaches Lectern through the published
port). Lectern has its own network namespace, so every new outbound connection is rejected:

```sh
PID=$(docker inspect -f '{{.State.Pid}}' lectern)
sudo nsenter -t "$PID" -n nft -f - <<'EOF'
table inet lectern_offline {
  chain output {
    type filter hook output priority 0; policy accept;
    oif "lo" accept
    ct state established,related accept
    reject
  }
}
EOF
```

Check that it holds. This runs outside Lectern's own guard, so only the firewall can refuse
it; *Connection refused* at once is the expected answer, a timeout or a connection means the
rules are not in place:

```sh
docker compose --env-file .env -f deploy/tailscale/compose.yaml exec lectern \
  python -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)"
```

(With the plain `compose.yaml`: `docker compose exec lectern python -c …`.)

Remove the rules with `sudo nsenter -t "$PID" -n nft delete table inet lectern_offline`.

The Tailscale rules were checked in a network namespace like the sidecar's: Lectern's user
was refused at once, root (as `tailscaled` runs) was not, and loopback worked. They were not
run against a live Tailscale deployment.
