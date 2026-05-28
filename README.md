# Flat Packager

Flat Packager turns a repository into one flat text archive, then rebuilds the
original folder structure from that archive.

The archive format is newline-delimited JSON. File contents are base64 encoded,
so text files, binary files, empty directories, and symlinks survive the round
trip. Restores validate recorded SHA-256 hashes before writing file contents.

## Install

From a checkout:

```bash
python3 -m pip install .
```

For development:

```bash
python3 -m pip install -e ".[dev]"
```

This installs two console commands:

```bash
flat-pack --help
flat-unpack --help
```

The repository also keeps compatibility wrappers:

```bash
python3 pack_repo.py --help
python3 unpack_repo.py --help
```

## Pack a Repository

Pack a local repository:

```bash
flat-pack /path/to/repo repo.flat.txt
```

Pack a public GitHub repository by shorthand or Git URL:

```bash
flat-pack owner/repo repo.flat.txt
flat-pack https://github.com/owner/repo.git repo.flat.txt
```

By default, local directory scans include all files and directories except
`.git`. To include only tracked git files:

```bash
flat-pack /path/to/repo repo.flat.txt --tracked-only
```

Useful options:

```bash
flat-pack /path/to/repo repo.flat.txt --exclude "node_modules/*"
flat-pack /path/to/repo repo.flat.txt --max-file-bytes 10485760
flat-pack /path/to/repo repo.flat.txt --include-git
```

## Restore a Repository

```bash
flat-unpack repo.flat.txt restored-repo
```

If the output directory already contains files:

```bash
flat-unpack repo.flat.txt restored-repo --overwrite
```

Validate an archive without writing files:

```bash
flat-unpack repo.flat.txt restored-repo --verify-only
```

On systems where symlink creation is unavailable:

```bash
flat-unpack repo.flat.txt restored-repo --no-symlinks
```

## Test

```bash
python3 -m unittest discover -s tests
```
