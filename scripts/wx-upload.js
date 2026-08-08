// 小程序代码上传（miniprogram-ci，免开微信开发者工具）。
// 用法: node scripts/wx-upload.js [版本号] [描述]
// 密钥: secrets/private.wxa5b11d3d8b80b07b.key（后台开发设置生成，gitignored）
'use strict';
const path = require('path');
const ci = require('miniprogram-ci');

(async () => {
  const project = new ci.Project({
    appid: 'wxa5b11d3d8b80b07b',
    type: 'miniProgram',
    projectPath: path.join(__dirname, '..', 'miniprogram'),
    privateKeyPath: path.join(__dirname, '..', 'secrets', 'private.wxa5b11d3d8b80b07b.key'),
    ignores: ['node_modules/**/*', 'README-miniprogram.md'],
  });
  const version = process.argv[2] || '0.1.0';
  const result = await ci.upload({
    project,
    version,
    desc: process.argv[3] || `哈尔密斯语音对话 v${version}`,
    setting: { es6: true, minify: true },
    robot: 1,
    onProgressUpdate: () => {},
  });
  console.log('UPLOAD-OK', JSON.stringify(result));
})().catch((e) => {
  console.error('UPLOAD-FAIL', e && (e.message || e));
  process.exit(1);
});
