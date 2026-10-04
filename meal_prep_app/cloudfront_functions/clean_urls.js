// Runs at the CloudFront edge on every viewer request. Lets pages be linked
// with clean paths (e.g. /admin) instead of the real S3 object key
// (/admin.html), without needing to rename or duplicate any files.
function handler(event) {
  var request = event.request;
  var uri = request.uri;

  // Leave directory-style requests ("/", "/foo/") and anything that already
  // names a file (has a "." in its last path segment, e.g. "/css/style.css")
  // untouched.
  if (uri.endsWith("/") || /\.[^/]+$/.test(uri)) {
    return request;
  }

  request.uri = uri + ".html";
  return request;
}
