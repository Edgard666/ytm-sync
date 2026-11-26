'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const form = $('login-form');
  const submit = $('login-submit');
  const errorBox = $('login-error');
  $('toggle-password').addEventListener('click', () => {
    const input = $('login-password');
    const hidden = input.type === 'password';
    input.type = hidden ? 'text' : 'password';
    $('toggle-password').textContent = hidden ? 'Masquer' : 'Afficher';
    $('toggle-password').setAttribute('aria-pressed', String(hidden));
  });
  form.addEventListener('submit', async e => {
    e.preventDefault();
    errorBox.hidden = true;
    submit.disabled = true;
    submit.firstChild.textContent = 'Connexion… ';
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({username: $('login-username').value, password: $('login-password').value})
      });
      if (!response.ok) {
        errorBox.textContent = response.status === 429 ? 'Trop de tentatives. Réessayez plus tard.' : 'Identifiants incorrects ou accès refusé.';
        errorBox.hidden = false;
        $('login-password').value = '';
        $('login-password').focus();
        return;
      }
      $('login-password').value = '';
      location.replace('/');
    } catch (_) {
      errorBox.textContent = 'Connexion au serveur impossible.';
      errorBox.hidden = false;
    } finally {
      submit.disabled = false;
      submit.firstChild.textContent = 'Se connecter ';
    }
  });
  fetch('/api/auth/session', {cache:'no-store',credentials:'same-origin'})
    .then(res => res.ok ? res.json() : null)
    .then(data => { if (data && data.authenticated) location.replace('/'); })
    .catch(() => {});
})();
