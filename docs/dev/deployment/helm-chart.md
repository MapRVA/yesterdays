# Helm Chart

Yesterdays is deployed as a [Kubernetes](https://kubernetes.io/) [helm chart](https://helm.sh/).
Below is a brief introduction to these tools, as well as specific instructions for configuring this Helm Chart for your needs.

## Kubernetes

Kubernetes is a powerful container orchestration platform for deploying applications.
If you are familiar with [Docker](https://www.docker.com/), Kubernetes runs the same basic abstraction, _containers_, to run workloads in a flexible way.
In Kubernetes, there are different resources such as [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/) or [Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) which define different aspects of how containers run: how many run at once, if and when they are restarted when they finish running or crash, what storage is mounted into them, etc.
A full tutorial on Kubernetes is out of scope for this documentation, but we recommend you reference the [excellent Kubernetes documentation](https://kubernetes.io/docs/setup/), and [get in touch with us](/contact/) for Yesterdays-specific support.

We understand that choosing Kubernetes for this project was a trade-off.
Kubernetes requires more up-front configuration than other hosting systems.
However, it comes with a mature ecosystem of solutions which makes it ideal for running a Yesterdays instance for years to come.

## Helm Charts

Helm Charts could be thought of as packages for Kubernetes.
They are installable sets of Kubernetes resources which can be configured as a group using a YAML configuration file.
By distributing Yesterdays as a Helm Chart, sysadmins can install each version of Yesterdays onto their cluster in a consistent, repeatable way.
New versions of the Helm Chart provide a way for Yesterdays developers to define application code updates, database migrations, and microservice dependency changes as a single unit.
This allows for disparate Yesterdays instances to all behave the same way, and upgrade in lockstep.

## Required Operators

A common abstraction in Kubernetes are _operators_, which themselves are sets of resources which can manage instances of common components like databases or message brokers.

The Yesterdays Helm Chart currently requires two off-the-shelf operators:

- [CloudNativePG](https://cloudnative-pg.io/)
- [RabbitMQ Cluster Kubernetes Operator](https://www.rabbitmq.com/kubernetes/operator/operator-overview)

Please see those projects' documentation for information on how to install them onto your cluster.

## Serving Suggestions

Here are a couple of tools we find valuable when working with Kubernetes deployments.
These are not necessary but you should consider using them or similar tools to make your deployment process easier.

- [Flux](https://fluxcd.io/) is a really neat GitOps tool for managing your Kubernetes resources. It lets you manage all of the resources you want installed on your cluster in a git repository, where commits immediately get applied to your cluster. We strongly recommend using either Flux or [ArgoCD](https://argoproj.github.io/cd/) for deploying Yesterdays and its required operators.
- [k9s](https://k9scli.io/) is a really slick tool for managing your cluster from the command-line. It is way more convenient than `kubectl` commands to monitor resources on your cluster.

## Values

This section describes the required values for the Yesterdays Helm Chart.
For a complete list, please see [the upstream `values.yaml`](https://github.com/MapRVA/helm-charts/blob/main/charts/yesterdays/values.yaml).

### `osm`

The `osm` key contains values related to OpenStreetMap authentication.
See [here](/deployment/auth/) for instructions on how to get these values.
Generate your own `clientSecret`, we recommend generating a 30-character password using a password generator.

```yaml
osm:
  clientId: <CLIENT ID>
  clientSecret: <CLIENT SECRET>
  secretKey: <SECRET KEY>
  redirectUri: https://example.org/auth/callback/
```
  
### `django`

[Django](https://www.djangoproject.com/) is the framework used to develop Yesterdays.
These are some basic, site-wide settings that are important to configure per-instance.

```yaml
django:
  timeZone: "America/New_York"
  secretKey: <SECRET KEY> # generate your own secret key
  allowedHosts: "yesterdays.today" # comma-separated list
  adminUsernames: "jacobwhall" # comma-separated list, spaces allowed
```

## Need Help?

We know deploying a Helm Chart into a Kubernetes cluster can be a technical process.
**Please [get in touch with us](/contact) and we'll walk you through it.**
We're excited to have you in our community!
