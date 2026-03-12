function drawChart(history){

let labels = []
let prices = []

history.forEach((h,i)=>{

labels.push(i)
prices.push(h.price)

})

const ctx = document.getElementById("priceChart")

if(window.marketChart){
window.marketChart.destroy()
}

window.marketChart = new Chart(ctx,{

type:"line",

data:{
labels:labels,

datasets:[{
label:"Historical Price",
data:prices,
borderColor:"#4CAF50",
tension:0.2
}]

},

options:{
responsive:true,
plugins:{
legend:{display:true}
}
}

})

}
