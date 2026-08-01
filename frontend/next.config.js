/** @type {import('next').NextConfig} */
module.exports = {
  reactStrictMode: true,

  // Handled by the router BEFORE any page is compiled, so "/" costs no page
  // compile and no extra round trip on cold start.
  async redirects() {
    return [{ source: "/", destination: "/dashboard", permanent: false }];
  },
};
