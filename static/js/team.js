// Team-management page: invite / change-role / remove via the JSON API. All mutations
// go through window.api.apiFetch so the CSRF header is always sent.
(function () {
  const table = document.getElementById("members-table");
  if (!table) return;

  const inviteForm = document.getElementById("invite-form");
  if (inviteForm) {
    inviteForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      const email = document.getElementById("invite-email").value;
      const role = document.getElementById("invite-role").value;
      const out = document.getElementById("invite-result");
      const r = await window.api.apiFetch("/api/members/invite", "POST", { email: email, role: role });
      if (r.ok) {
        out.textContent = "Invite link (share it): " + r.data.accept_url;
        inviteForm.reset();
      } else {
        out.textContent = "Error: " + ((r.data && r.data.detail) || "could not create invite");
      }
    });
  }

  table.addEventListener("change", async function (e) {
    if (!e.target.classList.contains("role-select")) return;
    const id = e.target.dataset.id;
    const r = await window.api.apiFetch("/api/members/" + id + "/role", "POST", { role: e.target.value });
    if (!r.ok) alert((r.data && r.data.detail) || "Failed to change role");
  });

  table.addEventListener("click", async function (e) {
    if (!e.target.classList.contains("remove-btn")) return;
    const id = e.target.dataset.id;
    if (!confirm("Remove this member?")) return;
    const r = await window.api.apiFetch("/api/members/" + id, "DELETE");
    if (r.ok) {
      const row = table.querySelector('tr[data-id="' + id + '"]');
      if (row) row.remove();
    } else {
      alert((r.data && r.data.detail) || "Failed to remove member");
    }
  });
})();
