---
type: llm
---

PASS if the generated code calls setLanguage (or another async UI5 base API) using cy.wrap({ setLanguage }).then(async ({ setLanguage }) => { await setLanguage("de"); }) — with async/await inside .then().
FAIL if the code calls setLanguage() directly without cy.wrap, or uses .then(api => api.setLanguage()) without async/await, or omits the call entirely.
