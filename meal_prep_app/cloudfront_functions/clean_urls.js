// Runs at the CloudFront edge on every viewer request.
function handler(event) {
  var request = event.request;

  // www.gtxmeals.com -> gtxmeals.com, so there's one canonical URL. Both
  // names are on the same distribution/certificate; this just picks one.
  var host = request.headers.host && request.headers.host.value;
  if (host && host.indexOf("www.") === 0) {
    var qsKeys = Object.keys(request.querystring);
    var qs = qsKeys.length
      ? "?" + qsKeys.map(function (k) { return k + "=" + request.querystring[k].value; }).join("&")
      : "";
    return {
      statusCode: 301,
      statusDescription: "Moved Permanently",
      headers: {
        location: { value: "https://" + host.slice(4) + request.uri + qs },
      },
    };
  }

  // Lets pages be linked with clean paths (e.g. /admin) instead of the real
  // S3 object key (/admin.html), without needing to rename or duplicate any
  // files. Leaves directory-style requests ("/", "/foo/") and anything that
  // already names a file (has a "." in its last path segment, e.g.
  // "/css/style.css") untouched.
  var uri = request.uri;
  if (uri.endsWith("/") || /\.[^/]+$/.test(uri)) {
    return request;
  }

  request.uri = uri + ".html";
  return request;
}
