async function loadMap(){

let res=await fetch("http://127.0.0.1:5000/api/territories")

let data=await res.json()

let terr=data._embedded.territories

let map=document.getElementById("map")

map.innerHTML=""

terr.forEach(t=>{

let cell=document.createElement("div")

cell.style.width="30px"
cell.style.height="30px"
cell.style.background="#2e8b57"

cell.title="Territory "+t.num

map.appendChild(cell)

})

}
