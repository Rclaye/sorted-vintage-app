# sorted-vintage-app

Service code for **Sorted Vintage**, a peer-to-peer resale marketplace: sign up, list clothes with photos, buy in Stripe test mode, and notify the seller.

Planning, decisions and infrastructure live in [sorted-vintage-infra](https://github.com/Rclaye/sorted-vintage-infra).

## Services (from Sprint 1)

| Service | Stack | Purpose |
| --- | --- | --- |
| bff | FastAPI, Jinja, HTMX | Pages and session |
| listings | Spring Boot | Items, photos, search, reservations |
| orders | FastAPI | Checkout orchestration |
| payments | FastAPI | Stripe Connect Express, webhooks, ledger |
| notifications | FastAPI worker | Email from RabbitMQ events |

## Pipeline

GitHub Actions builds each service on arm64 runners, scans it, publishes the image to GitHub Container Registry and commits the new tag to `sorted-vintage-gitops`. It never touches the cluster ([ADR-0002](https://github.com/Rclaye/sorted-vintage-infra/blob/main/docs/adr/0002-git-is-the-only-path-to-the-cluster.md)).
