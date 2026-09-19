# Reproducibility

Run the privacy-safe demo (SAMPLE inputs only):

```bash
scripts/trajectory demo portfolio
```

The demo writes a complete artifact set under the chosen runtime root and prints the cockpit. It never reads private data, never publishes and never performs a Git release write.

Canonical quality gate:

```bash
bash scripts/quality.sh
```
