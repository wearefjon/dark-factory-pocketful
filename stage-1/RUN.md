# Pocketful — Stage 1

Payments and settlements HTTP service (FastAPI + in-memory state).

## Build and run

From the repository root (`pocketful-result`):

```sh
docker build -t pocketful-stage-1 ./stage-1 && docker run --rm -p 8080:8080 -e PORT=8080 pocketful-stage-1
```

The service listens on `0.0.0.0:8080` (override with `-e PORT=<port>` and a matching `-p` mapping).
All runtime dependencies are installed during `docker build`; the container needs no outbound
network at run time.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | TCP port to listen on |

## Endpoints

- `GET /health`
- `POST /_test/reset`, `GET /_test/export`, `POST /_test/import` (unauthenticated test control)
- `POST /auth/signup`, `POST /auth/login`
- `GET /me`
- `POST /payments`
- `POST /requests`, `GET /requests`, `POST /requests/{id}/pay`, `POST /requests/{id}/decline`, `POST /requests/{id}/cancel`
- `POST /splits`
- `GET /activity`
- `POST /settlements`

State is held in memory and is intentionally ephemeral; use export/import to move it between
processes. `POST /_test/reset` replaces all state with the supplied fixture.
