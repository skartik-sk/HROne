# REVIEW.md

List of my findings: 

1:
where  mongo client 
what no timezone aware 
how default time is always utc but in docs mentioned all in IST hinted in R1
fix added tz_aware=tue

2;
where: health check
What: no db check in health 
How: funny but health check was too small 
Fix: ping db and for normal ping added new route

3: 
where: in compute_late_minutes function 
What:the calulaiton missing sec cal, no overnight shift case, and IST timezone issue 
How: followed from rule R2 then look dry run cases given there then found issues
Fix: added shift end also to calculete overnight , then calcuate base on sec, then return results.

4:
where: in compute_work_hours function
What: the calcualtion was not handinling half up with round()
How: got bolded half up mention in doc and in funtion no where mention R4
Fix: made new formula that take care of half up 

5: 
where: in compute_overtime function 
What: it was cheivn for 1 min not 30 and no overntigh work
How: 1st the claculation was havein some erros liek /60 hit me
Fix: take two candidate shift-end ,  pick nearest to punch_out

6:
where: in punch in endpoint
What: unknown emp, no timezone , no overnight 
How: returning 500 and wrong date 
Fix: ist date fn , duplicatekey error,


7:
where: Create employee fn 
What: Race condition create then find and then incert
How: find by ai 
Fix: unique index , time zone fix


8:
where: in list_empl
What: no sort,skip was off by one value
How: pageing issue tha ti found
Fix: first sort then page value tracking 


9:
where: indexes
What: there were no idexes for _id
How: as asked in decisions
Fix: Created 4 indexes emp_code and department of employee and emp_code and date  attendance log  


