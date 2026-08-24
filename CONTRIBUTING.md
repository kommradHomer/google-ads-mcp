# How to Contribute

We'd love to accept your patches and contributions to this project. There are
just a few small guidelines you need to follow.

## About this fork

This repository is a fork of
[googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp). It adds
per-user manager-account resolution, a `search_batch` tool, richer API error
messages and email-attributed auth logging on top of the upstream server. Changes
to the upstream code are licensed under the same Apache 2.0 license; no
Contributor License Agreement is required to contribute here. If you want a
change to land in Google's upstream project, open it there and follow their CLA
process instead.

See [DEPLOYMENT.md](DEPLOYMENT.md) for how to host the server.

## Code reviews

All submissions, including submissions by project members, require review. We
use GitHub pull requests for this purpose. Consult
[GitHub Help](https://help.github.com/articles/about-pull-requests/) for more
information on using pull requests.

## Code Style

This library conforms to [PEP 8](https://www.python.org/dev/peps/pep-0008/)
style guidelines and enforces an 80 character line width. It's recommended that
any contributor run the auto-formatter [`black`](https://github.com/psf/black).
To get started, first install `nox` and `black`:

```
pip install -e .[dev]
```

Then run the formatter on all Python files:

```
nox -s format
```

## Test changes

1.  Add or update unit tests in the `tests` directory.

1.  Run the unit tests for the supported Python versions that are available in
    your environment:

    ```
    nox -s tests*
    ```

### Test using Antigravity

To test changes by issuing prompts in Antigravity, configure the `google-ads-mcp` entry in your Antigravity client configuration (refer to the docs at [https://antigravity.google/docs/mcp](https://antigravity.google/docs/mcp)) so Antigravity runs the server using your local source files.

Replace `PATH_TO_REPO` in the following snippet with the path where you cloned the repo:

```
      "command": "PATH_TO_REPO/.venv/bin/google-ads-mcp",
```

When running the `agy` command from a terminal, add the `--debug` option so Antigravity outputs debug information as it processes prompts.

### Test from GitHub

After you push changes to GitHub, use `pipx` to run the server for a specific
branch, and use the `--no-cache` option so `pipx` gets the
latest changes.

Here's an example of an `mcpServers` entry that runs the latest code from a
branch named `awesome-feature-42` in this repo:

```json
{
  "mcpServers": {
    "ads-mcp": {
      "command": "pipx",
      "args": [
        "run",
        "--no-cache",
        "--spec",
        "git+https://github.com/YOUR_ORG/google-ads-mcp.git@awesome-feature-42",
        "google-ads-mcp"
      ]
    }
  }
}
```
