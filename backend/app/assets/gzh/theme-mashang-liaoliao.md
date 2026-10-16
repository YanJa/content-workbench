# 公众号排版组件库 —— 码上聊聊

> **使用说明**：本组件库为「码上聊聊」主题（暖橙品牌风），所有组件使用**内联样式**，可直接复制粘贴到微信公众号编辑器。
>
> **设计风格**：暖橙红品牌色（`#FF8A3D → #FF3B30` 渐变）+ 大圆角 + 柔和暖底。核心是**品牌素材组件**：气泡分隔符、带序号章节条、文中钩子卡、开头/文末关注卡、文末三连条——它们来自品牌视觉系统（`backend/scripts/make-kit.py`），原为 PNG 素材，本库已用**内联 HTML/CSS 等比复现**，因此能随文自适应、不受图片尺寸限制。
>
> **公众号平台限制须知**：
> - ❌ 不支持 `<style>`/`<script>`、CSS class/id、`position:fixed/absolute`、`float`、`@media`/`@keyframes`、`display:grid`
> - ✅ 支持内联 `style`、`display:flex`（有限）、内联 `<svg>`、`linear-gradient`、`border-radius`、`box-shadow`、`<section>/<p>/<span>/<strong>/<img>` 等基础标签
>
> **WeChat 兼容铁律**（本主题组件全部已按此写好，改动时必须遵守）：
> - 所有"装饰性空元素"（渐变分隔条、圆点、END 短线、三连条圆点）**必须在内部放 `<span leaf=""><br></span>` 占位**，否则微信会剥掉样式
> - **不要把 `font-size`/`border-bottom` 打在 `<strong>` 上**，也不要在同一个 `<p>` 里混多个不同 `font-size`——微信编辑器会自动"纠正"导致样式被重写。正确做法：拆成多个 `<p>`，每个 `<p>` 只有一个字号；高亮样式统一挂在外层 `<span>` 上
> - 不用 `position:absolute` 做划线/高亮；结构化区域没有内容时**整块删掉**，不留空 section

---

## 设计变量速查表

```
品牌主色：     #FF3B30（正红橙，锚点/序号/强调）
品牌暖色：     #FF8A3D（暖橙，辅助/渐起点）
品牌渐变：     linear-gradient(135deg,#FF8A3D 0%,#FF3B30 100%)
中间过渡色：   #FF6A3D（圆点/中间态）
浅暖底色：     #FFF3ED（开头引导卡渐变起）
浅暖底色 2：   #FFE8E5（开头引导卡渐变止）
极浅暖底：     #FFF8F4（文末关注卡底）
暖色描边：     rgba(255,138,61,0.22)
标题色：       #1B1D22（近黑）
正文色：       #3F3F3F（深灰）
辅助文字色：   #6E7385（中灰）
弱化文字色：   #8B90A0（浅灰）
三连条文字：   #5A5F6B
分割线色：     rgba(0,0,0,0.08)
正文字号：     15px（不可改）
行高：         1.75~1.8
字间距：       0.5px
最大宽度：     677px
内容区边距：   0 10px（左右各 10px）
```

字体栈：`-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif`

---

## 组件 1 全局容器

```html
<section style="max-width:677px;margin:0 auto;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;color:#3F3F3F;line-height:1.8;letter-spacing:0.5px;overflow-x:hidden;">

  <!-- 所有组件放在这里 -->

</section>
```

---

## 组件 2 开头关注卡（浅暖渐变底 + 品牌图标 + 关注按钮）

> **文案策略**：开头卡的作用是"3 秒说清这个号能给你什么"，不是求关注。第二行是号的一句话简介，第三行是行动号召按钮。简介和按钮文案按公众号实际定位填，不确定时保留品牌默认值（见下）。
>
> **`{{号名}}` / `{{一句话简介}}` 必须按实际公众号替换**；品牌默认值为「码上聊聊」/「聊 AI，聊工具，聊那些让生活更省事的小东西」。
>
> ⚠️ **本组件默认不装配**：外层 `<section>` 已加标记注释 `<!--OPENING_FOLLOW_CARD-->`，
> 排版后由后端整段剥离（本号只在文末放一次关注卡）。**照抄组件时请连同标记一起保留**，
> 不要自己删掉；后端另有提示词指令要求不输出开头卡，二者互为兜底。

```html
<!--OPENING_FOLLOW_CARD-->
<section style="margin:10px 10px 28px;padding:20px 22px;border-radius:18px;background:linear-gradient(135deg,#FFF3ED 0%,#FFE8E5 100%);border:1px solid rgba(255,138,61,0.22);">
  <section style="display:flex;align-items:center;gap:18px;">
    <section style="width:62px;height:62px;border-radius:50%;background:linear-gradient(135deg,#FF8A3D 0%,#FF3B30 100%);flex-shrink:0;display:flex;align-items:center;justify-content:center;">
      <svg viewBox="0 0 400 400" width="34" height="34"><path d="M120 96 h160 a42 42 0 0 1 42 42 v72 a42 42 0 0 1 -42 42 h-76 l-46 46 v-46 h-38 a42 42 0 0 1 -42 -42 v-72 a42 42 0 0 1 42 -42 z" fill="#FFFFFF"/><rect x="136" y="142" width="128" height="28" rx="11" fill="#FF3B30" opacity="0.92"/><rect x="136" y="184" width="88" height="28" rx="11" fill="#FF3B30" opacity="0.92"/></svg>
    </section>
    <section style="flex:1;min-width:0;">
      <p style="margin:0;font-size:19px;font-weight:800;color:#1B1D22;letter-spacing:-0.3px;line-height:1.5;">
        <span leaf="">你好，这里是 </span><span style="color:#FF3B30;"><span leaf="">{{号名}}</span></span>
      </p>
      <p style="margin:6px 0 0;font-size:14px;color:#6E7385;line-height:1.6;font-weight:500;">
        <span leaf="">{{一句话简介}}</span>
      </p>
      <p style="margin:10px 0 0;">
        <span style="display:inline-block;padding:6px 16px;border-radius:20px;background:linear-gradient(135deg,#FF8A3D 0%,#FF3B30 100%);font-size:13px;font-weight:700;color:#FFFFFF;letter-spacing:0.5px;"><span leaf="">关注我，每周更新 ↗</span></span>
      </p>
    </section>
  </section>
</section>
<!--/OPENING_FOLLOW_CARD-->
```

---

## 组件 3 章节条（渐变竖条 + 序号 + 标题 + 右侧细线）

> 本主题的**标志性章节标题**——左侧橙红渐变竖条 + 小号橙色序号 + 大号黑体标题 + 向右延伸的细线。序号按 `##` 顺序 `01/02/03…`，结语用 `∞`。

```html
<section style="margin:38px 0 20px;display:flex;align-items:center;">
  <section style="width:8px;height:30px;border-radius:5px;background:linear-gradient(135deg,#FF8A3D 0%,#FF3B30 100%);flex-shrink:0;"></section>
  <p style="margin:0 0 0 12px;font-size:13px;font-weight:700;color:#FF3B30;letter-spacing:2px;opacity:0.8;flex-shrink:0;"><span leaf="">01</span></p>
  <p style="margin:0 0 0 12px;font-size:21px;font-weight:800;color:#1B1D22;letter-spacing:-0.3px;flex-shrink:0;"><span leaf="">为什么值得试</span></p>
  <section style="flex:1;height:1px;background:rgba(0,0,0,0.08);margin-left:16px;"><span leaf=""><br></span></section>
</section>
```

> 窄标题时右侧线自动占满剩余空间；标题过长时把右侧线 `<section>` 整个删掉，避免挤压。**`<p>` 内必须只有一个字号**，序号与标题是两个独立 `<p>`。

---

## 组件 4 分隔符（4 种，按场景选一种）

> 四种分隔符来自品牌素材 `divider-*.png`，按"打断强度"从强到弱排列。**同一篇文章只用一种**，保持一致。

### 4a. 三点分隔（呼应 Logo 气泡，最常用）

```html
<section style="margin:34px 0;display:flex;align-items:center;justify-content:center;">
  <section style="width:150px;height:2px;border-radius:1px;background:linear-gradient(90deg,rgba(255,138,61,0),rgba(255,59,48,0.55));"><span leaf=""><br></span></section>
  <section style="width:12px;height:12px;border-radius:50%;background:#FF8A3D;opacity:0.45;margin:0 0 0 14px;"><span leaf=""><br></span></section>
  <section style="width:16px;height:16px;border-radius:50%;background:#FF6A3D;margin:0 14px;"><span leaf=""><br></span></section>
  <section style="width:12px;height:12px;border-radius:50%;background:#FF3B30;opacity:0.6;"><span leaf=""><br></span></section>
</section>
```

### 4b. 气泡分隔（品牌图形入正文，识别度最高）

```html
<section style="margin:34px 0;display:flex;align-items:center;justify-content:center;">
  <section style="width:130px;height:3px;border-radius:2px;background:linear-gradient(90deg,rgba(255,138,61,0),rgba(255,138,61,0.6));"><span leaf=""><br></span></section>
  <svg viewBox="0 0 400 400" width="30" height="30" style="margin:0 16px;transform:scaleX(-1);"><path d="M120 96 h160 a42 42 0 0 1 42 42 v72 a42 42 0 0 1 -42 42 h-76 l-46 46 v-46 h-38 a42 42 0 0 1 -42 -42 v-72 a42 42 0 0 1 42 -42 z" fill="#FF8A3D" opacity="0.55"/></svg>
  <svg viewBox="0 0 400 400" width="38" height="38" style="margin:0 16px;"><path d="M120 96 h160 a42 42 0 0 1 42 42 v72 a42 42 0 0 1 -42 42 h-76 l-46 46 v-46 h-38 a42 42 0 0 1 -42 -42 v-72 a42 42 0 0 1 42 -42 z" fill="#FF3B30" opacity="0.9"/></svg>
  <section style="width:130px;height:3px;border-radius:2px;background:linear-gradient(90deg,rgba(255,59,48,0.6),rgba(255,59,48,0));"><span leaf=""><br></span></section>
</section>
```

### 4c. 短横分隔（最克制，不打断阅读）

```html
<section style="margin:34px 0;display:flex;align-items:center;justify-content:center;">
  <section style="width:76px;height:5px;border-radius:3px;background:linear-gradient(135deg,#FF8A3D 0%,#FF3B30 100%);"><span leaf=""><br></span></section>
</section>
```

### 4d. 波纹分隔（折线律动，比直线有活力）

```html
<section style="margin:34px 0;display:flex;align-items:center;justify-content:center;">
  <section style="width:7px;height:14px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:0.52;margin:0 4px;"><span leaf=""><br></span></section>
  <section style="width:7px;height:22px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:0.64;margin:0 4px;"><span leaf=""><br></span></section>
  <section style="width:7px;height:14px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:0.76;margin:0 4px;"><span leaf=""><br></span></section>
  <section style="width:7px;height:22px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:0.88;margin:0 4px;"><span leaf=""><br></span></section>
  <section style="width:7px;height:14px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:1;margin:0 4px;"><span leaf=""><br></span></section>
  <section style="width:7px;height:22px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:0.88;margin:0 4px;"><span leaf=""><br></span></section>
  <section style="width:7px;height:14px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:0.76;margin:0 4px;"><span leaf=""><br></span></section>
  <section style="width:7px;height:22px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:0.64;margin:0 4px;"><span leaf=""><br></span></section>
  <section style="width:7px;height:14px;border-radius:4px;background:linear-gradient(180deg,#FF8A3D,#FF3B30);opacity:0.52;margin:0 4px;"><span leaf=""><br></span></section>
</section>
```

---

## 组件 5 正文段落

```html
<p style="font-size:15px;color:#3F3F3F;line-height:1.8;letter-spacing:0.5px;margin:0 10px 18px;">
  <span leaf="">正文内容……</span>
</p>
```

---

## 组件 6 正文标记（5 种变体）

### 6a. 品牌色加粗（锚点层，全文 ≤5 处）

```html
<span style="color:#FF3B30;font-weight:700;"><span leaf="">核心结论</span></span>
```

### 6b. 暖橙下划线（最常用，本主题标志性标记）

```html
<span style="border-bottom:2px solid #FFC9B0;font-weight:600;"><span leaf="">关键词短语</span></span>
```

### 6c. 浅暖底高亮（次要关键词）

```html
<span style="background:#FFF0E8;padding:1px 6px;border-radius:4px;color:#C2410C;"><span leaf="">要点</span></span>
```

### 6d. 荧光笔（长句强调，底部半高亮）

```html
<span style="background:linear-gradient(180deg,transparent 58%,#FFD9C2 58%);font-weight:600;"><span leaf="">值得记住的一句话</span></span>
```

### 6e. 行内代码

```html
<span style="background:#F1F5F9;color:#FF3B30;padding:1px 6px;border-radius:4px;font-family:'SF Mono',Consolas,Monaco,monospace;font-size:14px;"><span leaf="">code</span></span>
```

---

## 组件 7 引言 / 引用块

### 7a. 开头引言卡（渐变边 + 浅暖底 + 大引号）

```html
<section style="margin:0 10px 28px;padding:22px 24px;border-radius:16px;background:#FFF8F4;border:1px solid rgba(255,138,61,0.20);">
  <p style="margin:0 0 10px;font-size:30px;line-height:0.6;color:#FF8A3D;font-weight:900;"><span leaf="">"</span></p>
  <p style="margin:0;font-size:16px;font-weight:800;color:#1B1D22;line-height:1.75;">
    <span leaf="">这里是全文核心金句，</span><span style="color:#FF3B30;"><span leaf="">关键结论标品牌色</span></span><span leaf="">，收尾。</span>
  </p>
</section>
```

> 金句与外标题视角错开（外标题卖"为什么点开"，金句卖"核心观点"）；**不要原样复述标题关键词**。同一 `<p>` 内如有不同样式，只允许**颜色/字重**不同，**不要混不同 font-size**。

### 7b. 引用高亮块（左竖条 + 浅暖底）

```html
<section style="margin:0 10px 24px;background:#FFF8F4;border-radius:0 10px 10px 0;border-left:4px solid #FF3B30;padding:14px 18px;">
  <p style="margin:0;font-size:15px;color:#1B1D22;line-height:1.8;font-weight:600;">
    <span leaf="">引用的内容或 Prompt</span>
  </p>
</section>
```

### 7c. 居中金句分隔（章节间的过渡）

```html
<p style="margin:28px 10px;text-align:center;font-size:15px;font-weight:700;color:#FF3B30;letter-spacing:1px;">
  <span leaf="">—— 一句话过渡 ——</span>
</p>
```

---

## 组件 8 文中钩子卡（渐变实底 + 白色气泡 + 双行文案）

> **本主题最有辨识度的组件**，来自 `hook-card.png`。插在"讲到关键处、值得引导后台回复/关注"的位置，全文 1~2 处足够，不要滥用。

```html
<section style="margin:0 10px 26px;padding:22px 24px;border-radius:16px;background:linear-gradient(135deg,#FF8A3D 0%,#FF3B30 100%);">
  <section style="display:flex;align-items:center;gap:16px;">
    <svg viewBox="0 0 400 400" width="46" height="46" style="flex-shrink:0;"><path d="M120 96 h160 a42 42 0 0 1 42 42 v72 a42 42 0 0 1 -42 42 h-76 l-46 46 v-46 h-38 a42 42 0 0 1 -42 -42 v-72 a42 42 0 0 1 42 -42 z" fill="#FFFFFF" opacity="0.95"/><rect x="136" y="142" width="128" height="28" rx="11" fill="#FF3B30" opacity="0.92"/><rect x="136" y="184" width="88" height="28" rx="11" fill="#FF3B30" opacity="0.92"/></svg>
    <section style="flex:1;min-width:0;">
      <p style="margin:0;font-size:17px;font-weight:800;color:#FFFFFF;line-height:1.4;"><span leaf="">完整提示词已整理好</span></p>
      <p style="margin:5px 0 0;font-size:14px;color:rgba(255,255,255,0.90);font-weight:500;"><span leaf="">后台回复「周报」直接领</span></p>
    </section>
  </section>
</section>
```

---

## 组件 9 提示 / 警示条

### 9a. 品牌色提示条（重要提醒）

```html
<section style="margin:0 10px 24px;background:#FFF3ED;border-radius:10px;border-left:4px solid #FF3B30;padding:14px 18px;">
  <p style="margin:0 0 6px;">
    <span style="display:inline-block;background:#FF3B30;color:#FFFFFF;font-size:11px;font-weight:700;padding:2px 10px;border-radius:4px;letter-spacing:1px;"><span leaf="">提示</span></span>
  </p>
  <p style="margin:0;font-size:14px;color:#3F3F3F;line-height:1.8;"><span leaf="">提示内容</span></p>
</section>
```

### 9b. 踩坑提示（灰底，风险/注意事项）

```html
<section style="margin:0 10px 24px;background:#F7F7F8;border-radius:10px;border-left:4px solid #8B90A0;padding:14px 18px;">
  <p style="margin:0 0 6px;">
    <span style="display:inline-block;background:#8B90A0;color:#FFFFFF;font-size:11px;font-weight:700;padding:2px 10px;border-radius:4px;letter-spacing:1px;"><span leaf="">注意</span></span>
  </p>
  <p style="margin:0;font-size:14px;color:#3F3F3F;line-height:1.8;"><span leaf="">注意事项内容</span></p>
</section>
```

---

## 组件 10 列表

### 10a. 渐变圆标列表（要点，圆点用品牌色）

```html
<section style="margin:0 10px 20px;">
  <p style="margin:0 0 10px;font-size:15px;color:#3F3F3F;line-height:1.8;">
    <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#FF8A3D;margin-right:10px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">第一条要点内容</span>
  </p>
  <p style="margin:0 0 10px;font-size:15px;color:#3F3F3F;line-height:1.8;">
    <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#FF6A3D;margin-right:10px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">第二条要点内容</span>
  </p>
  <p style="margin:0;font-size:15px;color:#3F3F3F;line-height:1.8;">
    <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#FF3B30;margin-right:10px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">第三条要点内容</span>
  </p>
</section>
```

### 10b. 数字序号列表（步骤/清单）

```html
<p style="margin:0 10px 12px;font-size:15px;color:#3F3F3F;line-height:1.8;">
  <span style="display:inline-block;background:#FFF3ED;color:#FF3B30;border-radius:5px;padding:1px 9px;margin-right:8px;font-weight:900;font-size:13px;"><span leaf="">01</span></span><span leaf="">第一步的内容</span>
</p>
```

---

## 组件 11 文末三连条（轻量，不喧宾夺主）

> 来自 `ending-bar.png`。三个彩色圆点 + 点赞/在看/转发 + 竖线 + 一句话。**放在文末签名区之前**。

```html
<section style="margin:30px 10px 8px;display:flex;align-items:center;justify-content:center;flex-wrap:wrap;">
  <p style="margin:0 24px 0 0;font-size:15px;font-weight:700;color:#5A5F6B;">
    <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#FF8A3D;margin-right:8px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">点赞</span>
  </p>
  <p style="margin:0 24px 0 0;font-size:15px;font-weight:700;color:#5A5F6B;">
    <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#FF6A3D;margin-right:8px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">在看</span>
  </p>
  <p style="margin:0 20px 0 0;font-size:15px;font-weight:700;color:#5A5F6B;">
    <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#FF3B30;margin-right:8px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">转发</span>
  </p>
  <section style="width:1px;height:20px;background:rgba(0,0,0,0.10);margin-right:20px;"><span leaf=""><br></span></section>
  <p style="margin:0;font-size:14px;color:#8B90A0;font-weight:500;"><span leaf="">你的支持是我更新的动力</span></p>
</section>
```

---

## 组件 12 文末关注卡（极浅暖底 + 图标 + 三条承诺 + 扫码提示）

> 来自 `follow-card-end.png`。**给具体承诺，不是空喊求关注**。三条承诺按公众号实际内容替换。

```html
<section style="margin:24px 10px 28px;padding:24px 24px 20px;border-radius:16px;background:#FFF8F4;border:1px solid rgba(255,138,61,0.20);">
  <section style="display:flex;align-items:center;gap:14px;">
    <section style="width:50px;height:50px;border-radius:50%;background:linear-gradient(135deg,#FF8A3D 0%,#FF3B30 100%);flex-shrink:0;display:flex;align-items:center;justify-content:center;">
      <svg viewBox="0 0 400 400" width="28" height="28"><path d="M120 96 h160 a42 42 0 0 1 42 42 v72 a42 42 0 0 1 -42 42 h-76 l-46 46 v-46 h-38 a42 42 0 0 1 -42 -42 v-72 a42 42 0 0 1 42 -42 z" fill="#FFFFFF"/><rect x="136" y="142" width="128" height="28" rx="11" fill="#FF3B30" opacity="0.92"/><rect x="136" y="184" width="88" height="28" rx="11" fill="#FF3B30" opacity="0.92"/></svg>
    </section>
    <section>
      <p style="margin:0;font-size:18px;font-weight:800;color:#1B1D22;"><span leaf="">{{号名}}</span></p>
      <p style="margin:4px 0 0;font-size:13px;color:#FF3B30;font-weight:600;"><span leaf="">{{slogan}}</span></p>
    </section>
  </section>

  <section style="margin-top:18px;">
    <p style="margin:0 0 9px;font-size:15px;color:#3F3F3F;font-weight:500;">
      <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#FF8A3D;margin-right:10px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">每周拆解 1 个真正能用的 AI 工具</span>
    </p>
    <p style="margin:0 0 9px;font-size:15px;color:#3F3F3F;font-weight:500;">
      <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#FF6A3D;margin-right:10px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">只写实测过的，不转述通稿</span>
    </p>
    <p style="margin:0;font-size:15px;color:#3F3F3F;font-weight:500;">
      <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#FF3B30;margin-right:10px;vertical-align:middle;"><span leaf=""><br></span></span><span leaf="">提示词 / 模板直接给，拿走就能用</span>
    </p>
  </section>

  <section style="margin-top:20px;display:flex;align-items:center;">
    <section style="flex:1;height:1px;background:rgba(0,0,0,0.07);"><span leaf=""><br></span></section>
    <p style="margin:0 14px;font-size:13px;color:#8B90A0;font-weight:500;"><span leaf="">扫码 / 长按关注</span></p>
    <section style="flex:1;height:1px;background:rgba(0,0,0,0.07);"><span leaf=""><br></span></section>
  </section>

  <!--QRCODE_SLOT-->
  <p style="margin:16px 0 0;text-align:center;"><span leaf="">{{二维码}}</span></p>
  <!--/QRCODE_SLOT-->
</section>
```

---

## 组件 13 图片容器

```html
<section style="background:#FFFFFF;border-radius:12px;padding:6px;border:1px solid rgba(0,0,0,0.06);box-shadow:0 4px 14px -4px rgba(255,138,61,0.18);margin:0 10px 8px;">
  <section style="margin:0;border-radius:8px;overflow:hidden;">
    <span leaf=""><img src="图片URL" style="max-width:100%;height:auto;display:block;margin:0 auto;"></span>
  </section>
</section>
<p style="font-size:12px;color:#8B90A0;text-align:center;margin:0 10px 24px;">
  <span leaf="">— 图片说明文字</span>
</p>
```

无说明时删掉说明 `<p>`。图片一律 `max-width:100%;height:auto;display:block;margin:0 auto`，**不用 `width:100%`**。

---

## 组件 14 尾部作者签名区（全文仅一处）

> 与文末关注卡**并存**：关注卡讲"这个号是什么"，签名区讲"我是谁"。

```html
<section style="margin:8px 10px 40px;padding-top:18px;border-top:1px solid rgba(0,0,0,0.07);">
  <p style="font-size:13px;color:#6E7385;line-height:1.9;margin:0;">
    <span leaf="">我是 {{作者名}}，{{一句话简介}}。</span>
  </p>
  <p style="font-size:13px;color:#8B90A0;line-height:1.9;margin:6px 0 0;">
    <span leaf="">如果这篇对你有用，欢迎点赞、在看、转发三连，我们下篇见。</span>
  </p>
</section>
```

---

## 完整文章模板骨架

> 顺序即装配顺序。**须与主题气质一致，不要套用其它主题骨架**。

```html
<section style="max-width:677px;...">          <!-- 组件 1 全局容器 -->

  组件 2  开头关注卡（可选，品牌号建议保留）
  组件 7a 开头引言卡（有金句时）
  正文首段（组件 5）

  组件 3  章节条 01 + 标题1
    组件 5 正文段落（每段 1~3 处 6b 下划线）
    [可选] 7b 引用块 / 10a 列表 / 9a 提示条
  [可选] 组件 8 文中钩子卡（全文 1~2 处）
  [可选] 组件 4x 分隔符（章节间，全文只用一种）

  组件 3  章节条 02 + 标题2
    ...（同上）
  ……

  组件 3  结语章节条（序号用 ∞）
    组件 5 结语正文
  组件 11 文末三连条
  组件 12 文末关注卡
  组件 14 尾部签名区
</section>
```

**位置原则**：开头卡在最前（价值前置）→ 引言卡次之 → 章节正文 → 钩子卡在"讲到关键处" → 分隔符只在章节间 → 三连条 + 关注卡 + 签名收尾（这三者顺序固定）。

---

## 视觉层级（3 层递进）

| 层级 | 作用 | 频率 | 手段 |
|------|------|------|------|
| 锚点层 | 最强锚点：产品名/核心金句/CTA | 全文 ≤5 处 | 6a 品牌色加粗、组件 8 钩子卡、组件 3 章节条 |
| 标记层 | 正文关键词 | 每段 1~3 处 | 6b 暖橙下划线（最高频） |
| 容器层 | 引用、提示、要点 | 按需 | 7b 引用块、9a/9b 提示条、10a 列表 |

---

## 文章类型 → 组件组合配方

| 文章类型 | 核心组件组合 | 点缀组件 |
|---|---|---|
| 教程 / 操作指南 | 3 章节条 + 5 段落 + 10b 数字列表 + 行内代码 | 9a 提示条、4a 三点分隔、8 钩子卡 |
| 工具盘点 / 测评 | 3 章节条 + 5 段落 + 10a 要点列表 + 13 图片 | 4b 气泡分隔、7b 引用块、9b 踩坑提示 |
| 观点 / 深度分析 | 7a 引言卡 + 3 章节条 + 5 段落 + 7b 引用块 | 4c 短横分隔、6d 荧光笔、7c 居中金句 |
| 案例复盘 / 数据 | 3 章节条 + 5 段落 + 10a 列表 + 13 图片 | 9a 提示条、4a 三点分隔 |
| 随笔 / 生活 | 7a 引言卡 + 5 段落 + 7b 引用块 | 4d 波纹分隔、6c 浅暖底高亮、7c 居中金句 |
| 新闻 / 快讯 | 3 章节条 + 5 段落 | 4c 短横分隔、9a 提示条 |

**通用收尾**：所有类型都以「组件 11 三连条 → 组件 12 关注卡 → 组件 14 签名区」结尾；开头卡（组件 2）品牌号建议保留，纯观点文可省略以保持冷峻。

---

## Markdown → 码上聊聊 映射规则

| Markdown | 组件 |
|---|---|
| `# 标题` | 不进正文（公众号有独立标题栏）；如需文中大标题用组件 3 章节条 |
| `## 标题` | 组件 3 章节条（自动编号 01/02/…，结语用 ∞） |
| `### 标题` | 组件 6a 品牌色加粗作子标题，或组件 10b 数字序号 |
| 正文段落 | 组件 5（**每段主动标 1~3 处 6b 下划线**） |
| `**加粗**` | 组件 6a 品牌色加粗 |
| `==高亮==` / `<u>下划线</u>` | 组件 6b 暖橙下划线 |
| `~~荧光笔~~` | 组件 6d |
| 开头 `> 引用` | 组件 7a 开头引言卡 |
| 非开头 `> 引用` | 组件 7b 引用高亮块 |
| ` ``` 代码/命令/Prompt ``` ` | 通用库 1a 深色 / 1b 浅色代码块 |
| 行内 `` `code` `` | 组件 6e 行内代码 |
| `![说明](url)` | 组件 13 图片容器（有说明才加说明行） |
| `---` / `***` | 组件 4x 分隔符（全文一种） |
| `- 项` | 组件 10a 渐变圆标列表 |
| `1. 项` | 组件 10b 数字序号列表 |
| 表格 | 用组件 10a 列表或 7b 引用块承载（本主题不做重表格） |

---

## 本主题专属红线

1. **`{{号名}}` / `{{slogan}}` / `{{简介}}` 必须按实际公众号替换**——品牌默认值为「码上聊聊」/「有问题，码上聊聊」/「聊 AI，聊工具，聊那些让生活更省事的小东西」。品牌号直接用默认值即可。
2. **钩子卡（组件 8）全文 ≤2 处**——它是强 CTA，滥用会显得叫卖。
3. **分隔符全文只用一种**——混用会破坏节奏一致性。
4. **`{{作者名}}` 保留占位**，除非原文已有署名；品牌号的关注卡可用默认号名。
5. 所有装饰性空元素（渐变条、圆点、竖线、SVG 容器）**必须带 `<span leaf=""><br></span>`**，否则微信剥样式。
6. **下划线必须逐段落实，列表项同样要标**（最容易漏的地方）：正文段落**和 10a 列表项**每个都要找 1~3 个关键短语加 6b 暖橙下划线。整篇产出的 `border-bottom:2px solid #FFC9B0` 出现次数应明显多于章节数；如果只有寥寥几处，说明漏标了，必须补齐。整段划线同样禁止（失去焦点）。
