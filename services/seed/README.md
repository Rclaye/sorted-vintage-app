# Seed job (sv-dev only, T051)

Fills `sv-dev` with demo data, and runs again safely:

1. **Sellers.** For each entry in Secret `seed-sellers` (Terraform, from `seed_sellers` in
   `secrets.auto.tfvars`), it creates or updates the Keycloak user through the `seed` client,
   which may only manage users. Users are never in a realm file.
   The verify seller comes from Secret `verify-seller` until `seed_sellers` lists it: it is
   created without a Stripe link, onboarded once by hand in the browser, and its account id then
   goes into `seed_sellers` so a rebuilt lab links it again.
2. **Stripe links.** It calls Payments `PUT /connected-accounts/{sub}`, which only the `seed`
   client may call in dev (`ACCOUNT_LINK_AZP=seed`; the route is off in prod). Stripe will not
   let the platform accept its terms for a seller, so each account was onboarded once by hand.
3. **Listings.** It deals `LISTINGS_TOTAL` (50) listings over the demo sellers. The verify seller
   gets none. For each seller it reads what is already published (Listings `GET /sellers/{sub}`
   needs no token; a title ends in its number, `#07`), then signs in through the BFF login form
   like a browser and creates, photographs and publishes only what is missing. Photos are PNGs
   the tool draws itself (`app/photos.py`).

It reaches `https://dev.sortedvtg.lan:8443` and `https://auth.sortedvtg.lan:8443` from inside
the cluster through Traefik's Service, keeping the names for TLS and the Host header, and
trusts only the lab's own certificate (`app/site.py`; Sprint 2 spike "In-cluster sign-in").

Every request prints `METHOD where -> status`, and the last line is `PASS` or `FAIL`.

## Running the tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest --cov
```
