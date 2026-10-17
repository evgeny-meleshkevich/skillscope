# SkillScope

**Vacancy analytics and curriculum gap analysis.**

SkillScope turns vacancy descriptions into an evidence-based view of IT skills and compares employer requirements with a training curriculum. Explore skill frequencies, inspect supporting excerpts, identify missing practical topics and export a report for review.

[Русская версия](README.ru.md) · [Portfolio case](https://evgeny-meleshkevich.github.io/work/skill-scope/?lang=en)

![SkillScope dashboard with a saved real vacancy sample; Russian interface](docs/images/overview.png)

## Features

- Collect vacancies from Arbeitnow and Remotive; use the HH API adapter with appropriate access, or import JSON.
- Filter by source, role, seniority, work format and publication age; inspect the original vacancy and skill evidence.
- Explore skill rankings, a role–skill matrix, employer distribution and comparable salary groups.
- Compare a curriculum with observed skills, adjust coverage levels and save curriculum versions.
- Export statistics as CSV, a skill chart as SVG and a fixed HTML report printable as PDF.
- Keep local SQLite snapshots and processing versions; schedule collection while the application is running.

## Run locally

Requirements: Python 3.11+, Node.js 22.12+ and npm. The interface is in Russian; English documentation does not imply an English interface.

```sh
./start.sh --demo
```

Open **http://127.0.0.1:8770**. The script installs dependencies and builds the interface. First-time installation requires internet access. Stop with Ctrl+C. Use `PORT=8771 ./start.sh --demo` for a different port.

Demo mode loads **six fictional vacancies**, using the Arbeitnow JSON schema, into a separate `data/demo` database. Publication timestamps are assigned when the example is loaded. They are sample records, not collected market data. Automatic collection is disabled; manual collection still uses external APIs if requested. Select Arbeitnow to explore the example. An example curriculum is available in `examples/curriculum.txt`.

```sh
./start.sh
```

Normal mode starts with its own empty database in `data/` and enables the collection scheduler by default. Select sources and review scheduling in the interface. HH access may require `HH_TOKEN`; optional `HH_USER_AGENT` identifies the application to the source. Set credentials in the process environment, never in source files. No external LLM or paid AI API is required.

## Analysis and scope

Skill extraction uses a local dictionary and contextual rules. Required skills, optional skills, mentions and source tags are separate. Labels can be wrong; excerpts allow review. Skill shares use all vacancies in the selected sample as the denominator, including vacancies without recognized skills.

Salary groups remain separate by currency, period and tax basis. An aggregate midpoint median requires at least 30 comparable ranges. History starts with local collection; disappearance from a limited feed does not establish hiring. Curriculum coverage measures the weighted share of observed skills covered by practice, not course quality or employment outcomes. Cross-source duplicates can remain.

See [detailed methodology in Russian](docs/METHODOLOGY.ru.md).

![Curriculum coverage using a sample training program](docs/images/course.png)

The screenshots show a saved real sample collected on 5 October 2026: 136 Arbeitnow and 11 Remotive listings. That historical database is not included. Screenshots do not represent the fictional quick-start dataset or current market conditions.

## Sources and data

Source identities and links remain visible in vacancy cards and reports. Each source has different geographic coverage and access restrictions. A failed source is shown as a failure, not replaced with another source's data. Collection is bounded and repeat requests are limited to a six-hour interval per source/configuration.

- [Arbeitnow API](https://www.arbeitnow.com/blog/job-board-api)
- [Remotive API and usage conditions](https://github.com/remotive-com/remote-jobs-api) — public feed with a 24-hour delay; attribution and original links are retained.
- [HH API](https://api.hh.ru/openapi/redoc) — access depends on source authorization and availability.

Retrieved vacancy text and raw responses remain local. Third-party vacancy content is not licensed by this repository. Check source terms before collecting or redistributing it.

## Development

```sh
.venv/bin/python -m pytest -q
npm run build --prefix frontend
```

Tests use isolated temporary databases and fixtures. The application binds to localhost; it is a single-user tool, without deployment authentication or a multi-user permission model.

```text
backend/       API, collectors, SQLite, extraction and analytics
frontend/      React, TypeScript, ECharts and styles
scripts/       Stored-data reprocessing and demo loader
tests/         Pipeline, calculations, exports and API checks
examples/      Fictional vacancies and sample curriculum
docs/          Screenshots and calculation methodology
start.sh       Local launcher
```

Runtime dependencies are declared in `requirements.txt` and `frontend/package.json`, with `requirements-lock.txt` and the frontend lockfile included. Dependencies retain their respective licenses. Installed packages, databases, credentials and generated reports are excluded from version control.
