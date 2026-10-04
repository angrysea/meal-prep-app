// Talks to Cognito's public unauthenticated API directly via fetch. No SDK
// needed: the app client has no secret, and SignUp/InitiateAuth/etc. are
// designed for exactly this kind of unsigned browser call.

const SESSION_KEY = "mealprep.session";

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

function saveSession(result) {
  localStorage.setItem(
    SESSION_KEY,
    JSON.stringify({
      idToken: result.IdToken,
      expiresAt: Date.now() + result.ExpiresIn * 1000,
    })
  );
}

export function getSession() {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) return null;
  const session = JSON.parse(raw);
  if (Date.now() >= session.expiresAt) {
    localStorage.removeItem(SESSION_KEY);
    return null;
  }
  return session;
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
