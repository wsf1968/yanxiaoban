const form=document.getElementById('pair-form');
const button=document.getElementById('pair-submit');
const status=document.getElementById('pair-status');
async function login(code){
  button.disabled=true;status.textContent='正在登录…';
  try{
    const response=await fetch('api/pair',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code})});
    const data=await response.json();
    if(!response.ok)throw new Error(data.message || '登录未完成');
    document.getElementById('pair-code').value='';location.replace('./'+location.search+location.hash);
  }catch(error){status.textContent=error.message;button.disabled=false;}
}
form.addEventListener('submit',event=>{
  event.preventDefault();login(document.getElementById('pair-code').value.trim());
});
// A cross-app navigation may initially omit a Strict cookie; same-origin fetch reuses it.
(async()=>{
  const fragment=new URLSearchParams(location.hash.slice(1));
  const code=fragment.get('login');
  if(code){
    fragment.delete('login');
    const remaining=fragment.toString();
    history.replaceState(null,'',location.pathname+location.search+(remaining?'#'+remaining:''));
  }
  try{
    const response=await fetch('api/status');
    if(response.ok){location.replace('./'+location.search+location.hash);return;}
  }catch(error){status.textContent='请确认运行言小办的电脑仍然在线。';return;}
  if(code)login(code);
})();
