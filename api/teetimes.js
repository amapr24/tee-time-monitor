const OWNER  = 'amapr24';
const REPO   = 'tee-time-monitor';
const BRANCH = 'main';
const FILE   = 'tee_times.html';

export default async function handler(req, res) {
  const url = `https://raw.githubusercontent.com/${OWNER}/${REPO}/${BRANCH}/${FILE}`;

  const ghRes = await fetch(url, {
    headers: {
      'Authorization': `Bearer ${process.env.GITHUB_TOKEN}`,
      'Accept': 'text/html',
    },
  });

  if (!ghRes.ok) {
    return res.status(ghRes.status).json({ error: `GitHub returned ${ghRes.status}` });
  }

  const html = await ghRes.text();
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.status(200).send(html);
}
