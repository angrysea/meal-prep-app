// Talks to Cognito's public unauthenticated API directly via fetch. No SDK
// needed: the app client has no secret, and SignUp/InitiateAuth/etc. are
// designed for exactly this kind of unsigned browser call.

const SESSION_KEY = "mealprep.session";
// Refresh slightly before actual expiry so a request started right before the
// deadline doesn't race an id token that expires mid-flight.
const REFRESH_SKEW_MS = 60_000;

async function cognitoRequest(region, action, body) {
  const res = await fetch(`https://cognito-idp.${region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.message || data.__type || `${action} failed`);
  }
  return data;
}

export async function signUp({ region, clientId, email, password, name }) {
  const attributes = [{ Name: "email", Value: email }];
  if (name) attributes.push({ Name: "name", Value: name });
  return cognitoRequest(region, "SignUp", {
    ClientId: clientId,
    Username: email,
    Password: password,
    UserAttributes: attributes,
  });
}

export async function confirmSignUp({ region, clientId, email, code }) {
  return cognitoRequest(region, "ConfirmSignUp", {
    ClientId: clientId,
    Username: email,
    ConfirmationCode: code,
  });
}

export async function resendConfirmationCode({ region, clientId, email }) {
  return cognitoRequest(region, "ResendConfirmationCode", {
    ClientId: clientId,
    Username: email,
  });
}

export async function forgotPassword({ region, clientId, email }) {
  return cognitoRequest(region, "ForgotPassword", {
    ClientId: clientId,
    Username: email,
  });
}

export async function confirmForgotPassword({ region, clientId, email, code, newPassword }) {
  return cognitoRequest(region, "ConfirmForgotPassword", {
    ClientId: clientId,
    Username: email,
    ConfirmationCode: code,
    Password: newPassword,
  });
}

export async function signIn({ region, clientId, email, password }) {
  const data = await cognitoRequest(region, "InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: clientId,
    AuthParameters: { USERNAME: email, PASSWORD: password },
  });
  saveSession(data.AuthenticationResult);
  return data.AuthenticationResult;
}

function loadRawSession() {
  const raw = localStorage.getItem(SESSION_KEY);
  return raw ? JSON.parse(raw) : null;
}

function saveSession(result) {
  const existing = loadRawSession();
  localStorage.setItem(
    SESSION_KEY,
    JSON.stringify({
      idToken: result.IdToken,
      // REFRESH_TOKEN_AUTH doesn't issue a new refresh token by default -
      // the original one stays valid, so keep it if this response omits one.
      refreshToken: result.RefreshToken || (existing && existing.refreshToken) || null,
      expiresAt: Date.now() + result.ExpiresIn * 1000,
    })
  );
}

// Synchronous, cache-only read - may return an already-expired session.
// Fine for quick UI checks (e.g. nav rendering); use getValidSession() before
// anything that actually calls the API with the token.
export function getSession() {
  return loadRawSession();
}

// Returns a session with a live id token, transparently using the refresh
// token to renew it if it's expired (or about to). Returns null, and signs
// out, if there's no session or the refresh token itself is no longer valid.
export async function getValidSession({ region, clientId }) {
  const session = loadRawSession();
  if (!session) return null;
  if (Date.now() < session.expiresAt - REFRESH_SKEW_MS) return session;
  if (!session.refreshToken) {
    signOut();
    return null;
  }
  try {
    const data = await cognitoRequest(region, "InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: clientId,
      AuthParameters: { REFRESH_TOKEN: session.refreshToken },
    });
    saveSession(data.AuthenticationResult);
    return loadRawSession();
  } catch (err) {
    signOut();
    return null;
  }
}

export function signOut() {
  localStorage.removeItem(SESSION_KEY);
}

function decodeIdToken(idToken) {
  const payload = idToken.split(".")[1];
  const json = decodeURIComponent(
    atob(payload.replace(/-/g, "+").replace(/_/g, "/"))
      .split("")
      .map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"))
      .join("")
  );
  return JSON.parse(json);
}

export function isAdmin(session) {
  if (!session) return false;
  const claims = decodeIdToken(session.idToken);
  const groups = claims["cognito:groups"] || "";
  return String(groups).includes("Admins");
}
