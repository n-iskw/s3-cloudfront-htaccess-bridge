const fs = require('fs');
const path = require('path');
const { minify } = require('terser');

const ROOT_DIR = path.resolve(__dirname, '..');
const SOURCE_PATH = path.join(ROOT_DIR, 'cloudfront-function', 'handler.js');
const MINIFIED_PATH = path.join(ROOT_DIR, 'cloudfront-function', 'handler.min.js');
const TEMPLATE_PATH = path.join(ROOT_DIR, 'infra', 'bridge-resources.yaml');
const MAX_SOURCE_BYTES = 10 * 1024;
const BEGIN_MARKER = '      # BEGIN GENERATED CLOUDFRONT FUNCTION CODE';
const END_MARKER = '      # END GENERATED CLOUDFRONT FUNCTION CODE';

async function build() {
  const source = fs.readFileSync(SOURCE_PATH, 'utf8');
  const result = await minify(source, {
    ecma: 2020,
    module: true,
    // CloudFront invokes the global `handler` entry point, which is not an
    // import/export-visible entry point to Terser. Keep top-level declarations
    // while still compressing and mangling their bodies.
    compress: {
      toplevel: false,
      unused: false,
    },
    mangle: {
      toplevel: false,
      keep_fnames: /^handler$/,
    },
    format: {
      comments: false,
      semicolons: true,
    },
  });

  if (!result.code) {
    throw new Error('Terser produced an empty CloudFront Function source');
  }

  const minified = `${result.code}\n`;
  const byteLength = Buffer.byteLength(minified, 'utf8');
  if (byteLength >= MAX_SOURCE_BYTES) {
    throw new Error(
      `CloudFront Function source is ${byteLength} bytes; it must be smaller than ${MAX_SOURCE_BYTES} bytes`,
    );
  }

  const template = fs.readFileSync(TEMPLATE_PATH, 'utf8');
  const begin = template.indexOf(BEGIN_MARKER);
  const end = template.indexOf(END_MARKER);
  if (begin === -1 || end === -1 || end <= begin) {
    throw new Error(`CloudFormation template is missing generated-code markers: ${TEMPLATE_PATH}`);
  }

  const generatedBlock = [
    BEGIN_MARKER,
    '      FunctionCode: |',
    ...minified.trimEnd().split('\n').map((line) => `        ${line}`),
    END_MARKER,
  ].join('\n');
  const updatedTemplate = `${template.slice(0, begin)}${generatedBlock}${template.slice(end + END_MARKER.length)}`;

  fs.writeFileSync(MINIFIED_PATH, minified);
  fs.writeFileSync(TEMPLATE_PATH, updatedTemplate);
  console.log(`CloudFront Function: ${byteLength} bytes (limit ${MAX_SOURCE_BYTES})`);
}

if (require.main === module) {
  build().catch((error) => {
    console.error(`CloudFront Function build failed: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = { build, MAX_SOURCE_BYTES };
