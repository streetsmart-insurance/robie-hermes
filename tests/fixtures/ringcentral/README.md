# Sanitized RingCentral workbook fixture data

The JSON fixture mirrors the verified August 2026 Performance Report schemas
for the `Filters`, `Users`, `Queues`, and `Calls` worksheets. Tests build XLSX
files in temporary directories so no real RingCentral export is committed.
Every person, queue, extension, session, and number is fictional; phone values
use the reserved North American `555-01xx` range.
