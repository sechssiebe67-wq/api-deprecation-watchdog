# Product Criteria

## Hard Requirements (MUST)

Every product must satisfy these requirements before the Salesman can approve its development.

- **Web Deployable:** Must run as a web application and be accessible through a browser.
- **Originality:** Must not be a direct copy of an existing product, whether internally developed or commercially available. Existing concepts may be combined or significantly improved to create something unique.
- **Expandable:** Must have an architecture that allows additional features and future improvements.
- **Real User Value:** Must solve a real problem, provide entertainment, improve productivity, or offer another clearly identifiable benefit.
- **Clearly Defined Audience:** Must have at least one identifiable target audience.
- **Standalone:** Must function independently without requiring another AI Product Factory application.
- **Autonomous Operation:** Must operate without continuous human intervention after deployment.
- **Kubernetes Compatible:** Must be containerized and deployable on the existing k3s cluster using Helm and Argo CD.
- **Resource Efficient:** Must stay within defined CPU, RAM, and storage budgets.
- **Security:** Must follow basic security practices, including dependency scanning, safe secret handling, and input validation.
- **Testable:** Must support automated testing and have measurable acceptance criteria.
- **Maintainable:** Must contain readable source code, clear project structure, and sufficient technical documentation.
- **Legal Compliance:** Must respect software licences, copyright, privacy regulations, and applicable laws.
- **Health Monitoring:** Must expose a reliable way to verify whether the deployment is healthy.
- **Unique Product Identity:** Must have a unique repository name, product name, and subdomain.

## Soft Requirements (SHOULD)

These requirements should influence the Salesman's evaluation and ranking.

- **Database Independence:** Should preferably work without a database. A database is allowed when persistent storage adds meaningful value.
- **Monetization Potential:** Should have a realistic path to revenue, such as subscriptions, advertisements, one-time purchases, or paid features.
- **Low Operating Costs:** Should require minimal infrastructure, external APIs, and paid services.
- **Fast MVP Development:** Should ideally have a deployable minimum viable product within a limited development budget.
- **Low Maintenance:** Should require minimal ongoing maintenance and manual administration.
- **Market Differentiation:** Should offer a meaningful advantage over existing alternatives.
- **Growth Potential:** Should be able to attract additional users without a proportional increase in operating costs.
- **User Retention:** Should provide reasons for users to return regularly.
- **Discoverability:** Should be understandable, searchable, and easy to share.
- **Mobile Friendly:** Should work well on smartphones, tablets, and desktop browsers.
- **Accessibility:** Should follow established accessibility principles.
- **Minimal Dependencies:** Should avoid unnecessary external services and vendor lock-in.
- **Measurable Success:** Should provide metrics that help evaluate adoption, reliability, and product value.
- **Reusability:** Should preferably reuse existing components, design patterns, and deployment templates.
- **Innovation Potential:** Should have opportunities for unique features or future experimentation.

## Product Rejection Criteria

The Salesman must reject a product idea if:

- It is merely a clone without meaningful differentiation.
- Its purpose or target audience cannot be explained clearly.
- It cannot run within the available infrastructure budget.
- It requires unavailable credentials, licences, hardware, or external services.
- It introduces unacceptable security, legal, or privacy risks.
- It has no realistic path to a working MVP.
- It requires continuous manual operation.
- It offers no meaningful advantage over an already active product.
- Its projected value does not justify its development and operating costs.

## Product Lifecycle Requirements

Every approved product must support:

1. **Creation:** A dedicated GitHub repository is created.
2. **Development:** The AI Developer implements the MVP.
3. **Review:** Automated testing and AI code review validate the product.
4. **Deployment:** Helm and GitOps configuration deploy the application.
5. **Monitoring:** Operational health and product performance are evaluated.
6. **Iteration:** The product can receive additional features and improvements.
7. **Archival:** The repository is preserved while the deployment and Helm chart are removed.
8. **Reactivation:** An archived product can be rebuilt and deployed again if its potential improves.

The maximum number of simultaneously active product repositories is **10**.