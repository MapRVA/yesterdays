# Authentication

Yesterdays uses OpenStreetMap as an authentication provider.

In order to configure authentication for Yesterdays, you will need to create an OpenStreetMap account if you haven't already, and then navigate to [My Client Applications](https://www.openstreetmap.org/oauth2/applications) in your account settings.
Then, follow these instructions:

1. Click "Register new application"
2. Write a descriptive name, e.g. "MapRVA Yesterdays". This name will be visible to your users during the login flow.
3. Set the Redirect URI to be `/auth/callback/` on your instance's domain. E.g. `https://yesterdays.today/auth/callback/`.
4. No permissions are necessary leave all checkboxes unchecked and then click "Register".
5. You will be presented with a Client ID and Client Secret. Save these values before clicking away. You will need to add them to your Helm Chart configuration, see [here](/deployment/helm-chart/) for more information.
