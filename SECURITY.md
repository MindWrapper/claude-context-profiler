# Security

The profiler sits between Claude Code and the API, so it handles your prompts and your credentials.
Nothing it records leaves your machine: the recorder listens on `127.0.0.1` only, your API key and
sign-in token are blanked before anything is written, and the page loads nothing from the internet.

## Reporting a vulnerability

Please report security problems privately, not in a public issue: open the repository's **Security**
tab and choose **Report a vulnerability**. Include what you found, how to reproduce it, and which
version or commit you used. You will get an answer within a week.

Examples of what counts:

- a credential or token that reaches the disk, a log or the page
- the recorder or the page answering on anything other than `127.0.0.1`
- recorded text that can run as script in the page
- anything the page loads from the network

## Supported versions

Only the latest commit on `main` is supported.
