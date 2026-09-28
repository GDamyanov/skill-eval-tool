---
type: llm
---

PASS if the generated Cypress test code uses .realClick() and cy.realType() (from cypress-real-events) instead of .click() and .type() for user interactions.
FAIL if the code uses .click() or .type() for simulating user input, or if no Cypress test code is present.
