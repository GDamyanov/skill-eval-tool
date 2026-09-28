---
type: llm
---

PASS if the generated Cypress test code uses attribute selectors like [ui5-checkbox] (with square brackets) instead of tag selectors like ui5-checkbox (without brackets).
FAIL if the code uses bare tag selectors like cy.get("ui5-checkbox") without square brackets, or if no Cypress test code is present.
