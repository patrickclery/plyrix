# Security policy

## Reporting a problem

Report security problems privately through GitHub's private vulnerability reporting:

https://github.com/patrickclery/plyrix/security/advisories/new

Please don't open a public issue for them. Don't include tokens, private paths or library listings in a report; describe the setup with placeholders such as `/srv/music`.

The same form is the private channel for conduct reports. Other questions and concerns go in a normal issue.

## What counts

- any write outside a `.lrc` or `.txt` sidecar beside an audio file, and any change to an audio file
- a token leak, in output, logs, reports or the state database
- any request to a plex.tv host; plyrix talks to the local Plex Media Server only
- any lyric text outside a sidecar, such as in output, logs or reports

## Supported versions

Only the latest minor release gets fixes.
