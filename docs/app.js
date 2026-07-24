const navButtons = document.querySelectorAll("[data-view-target]");
const views = document.querySelectorAll("[data-view]");
const toast = document.querySelector(".toast");
let toastTimer;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
}

function showView(name) {
  views.forEach((view) => view.classList.toggle("active", view.dataset.view === name));
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.viewTarget === name);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

navButtons.forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.viewTarget));
});

document.querySelectorAll("[data-feedback]").forEach((button) => {
  button.addEventListener("click", () => {
    const group = button.closest(".feedback-actions");
    group.querySelectorAll("button").forEach((item) => item.classList.remove("selected"));
    button.classList.add("selected");
    const messages = {
      helpful: "已记录：这条推荐对你有用，后续会适度提高相似信号的优先级。",
      irrelevant: "已记录：这条与你无关，后续会减少相似噪音。",
      deep: "已加入深挖队列：下一份日报会优先补充官网、README 与使用建议。",
    };
    showToast(messages[button.dataset.feedback]);
  });
});

const relatedToggle = document.querySelector(".related-toggle");
const relatedList = document.querySelector("#related-list");
relatedToggle.addEventListener("click", () => {
  const expanded = relatedToggle.getAttribute("aria-expanded") === "true";
  relatedToggle.setAttribute("aria-expanded", String(!expanded));
  relatedList.hidden = expanded;
});

const goalDisplay = document.querySelector("#goal-display");
const goalEditor = document.querySelector("#goal-editor");
const goalText = document.querySelector("#goal-text");
const goalInput = document.querySelector("#goal-input");

function openGoalEditor() {
  goalDisplay.hidden = true;
  goalEditor.hidden = false;
  goalInput.focus();
}

function closeGoalEditor() {
  goalEditor.hidden = true;
  goalDisplay.hidden = false;
}

document.querySelectorAll("[data-edit-goal], [data-open-goal]").forEach((button) => {
  button.addEventListener("click", () => {
    if (window.innerWidth <= 1260) {
      showView("goals");
      showToast("目标编辑面板将在正式版中以抽屉形式打开。");
      return;
    }
    openGoalEditor();
  });
});

document.querySelector("[data-cancel-goal]").addEventListener("click", closeGoalEditor);
goalEditor.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = goalInput.value.trim();
  if (!value) {
    showToast("请先描述你现在希望追踪的目标。");
    return;
  }
  goalText.textContent = value;
  closeGoalEditor();
  showToast("目标已重新理解。下一轮信号会按照新目标进行筛选。");
});

document.querySelectorAll("#focus-chips button:not(.add-chip)").forEach((button) => {
  button.addEventListener("click", () => {
    const label = button.textContent.replace("×", "").trim();
    button.remove();
    showToast(`已暂时移除“${label}”，保存目标后正式生效。`);
  });
});

document.querySelector(".add-chip").addEventListener("click", () => {
  showToast("正式版会在这里提供自然语言添加和推荐标签。");
});

document.querySelectorAll("[data-demo-link]").forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    showToast("当前为视觉原型，接入数据库后会跳转到真实产品或原始来源。");
  });
});

document.querySelectorAll(".suggestions button").forEach((button) => {
  button.addEventListener("click", () => {
    const input = button.closest(".assistant-box").querySelector("textarea");
    input.value = button.textContent;
    input.focus();
  });
});

document.querySelector(".assistant-box > button").addEventListener("click", () => {
  const input = document.querySelector(".assistant-box textarea");
  if (!input.value.trim()) {
    showToast("先输入一句你想如何调整推荐。");
    input.focus();
    return;
  }
  showToast("已理解你的调整，正式版会展示目标变化供你确认。");
  input.value = "";
});

const date = new Date();
const weekdays = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];
document.querySelector("#today-date").textContent = `${date.getMonth() + 1} 月 ${date.getDate()} 日 · ${weekdays[date.getDay()]}`;
